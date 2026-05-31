"""Provision a Foundry **WorkflowAgent** that supervises the FinSight US specialists.

This makes the supervisor visible in the Foundry portal (Agents -> workflow ->
Playground). It mirrors the local Microsoft Agent Framework supervisor in
`orchestrator.py`, but runs entirely server-side.

Pattern (same as ../../pipeline/agents/create_workflow.py):
  1. Router agent classifies the user question -> emits comma-separated
     specialist keywords.
  2. One ConditionGroup per specialist; matching ones invoke the existing
     Foundry agents (finsight-us-financials / -external / -product / -meta).
  3. Synthesizer agent merges grounded answers into one final reply.

Usage:
  cd POC2
  python -m pipeline.agents.create_workflow

Env (.env auto-loaded):
  AZURE_AI_PROJECT_ENDPOINT
  FOUNDRY_MODEL_DEPLOYMENT       (e.g. gpt-4.1)
  FOUNDRY_AGENT_PREFIX           (default: 'finsight-us')
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, WorkflowAgentDefinition

from common import env
from agents.specs import AGENT_SPECS

PREFIX = env("FOUNDRY_AGENT_PREFIX", "finsight-us")
MODEL = env("FOUNDRY_MODEL_DEPLOYMENT", required=True)


# ---------------------------------------------------------------------------
# Specialist names (must match what create_agents.py provisioned)
# ---------------------------------------------------------------------------
SPECIALIST_NAMES = {s.role: f"{PREFIX}-{s.role}" for s in AGENT_SPECS}


# ---------------------------------------------------------------------------
# Router agent: classifies intent -> emits comma-separated keywords
# ---------------------------------------------------------------------------
ROUTER_NAME = f"{PREFIX}-router"

ROUTER_INSTRUCTIONS = """You are the FinSight US routing classifier.
The user asks a question; you decide which specialist agents should answer.

Specialists available (use the EXACT keyword in your reply):
- FINANCIALS  -> US monthly Financial Close decks. SOURCE OF TRUTH for any
                 US$ figure: Net Sales, Cost, Gross Margin, OPEX, Operating
                 Income, $ growth (YoY / vs PY / QoQ), monthly periods.
- EXTERNAL    -> IR Notes + Quarterly External Update decks. Public
                 messaging, guidance, pre-earnings narrative, Q&A talking
                 points, "what management said".
- PRODUCT     -> Brand MBRs + strategy + LRR pre-reads. NBRx / TRx / NRx /
                 market share / scripts / brand tactics / campaigns /
                 launch readiness.
- META        -> Cover pages, disclaimers, agendas, references.

Routing rules (DEFAULT: emit EXACTLY ONE keyword):
- ANY US $ figure (sales, $ growth, cost, margin, OPEX, revenue,
  "how much did X grow / sell", YoY / vs PY with $ implied) -> FINANCIALS
- Prescription metrics (NBRx / TRx / NRx / share / scripts / launch /
  campaigns / tactics) -> PRODUCT
- Public messaging / guidance / Q&A / IR narrative / press / what
  management said -> EXTERNAL
- Boilerplate (cover, disclaimer, agenda, references) -> META
- Fan-out ONLY when the user explicitly spans dimensions:
    "How is Leqvio doing in Q1?" (open-ended)        -> FINANCIALS, PRODUCT, EXTERNAL
    "Full update on Kisqali"                         -> EXTERNAL, PRODUCT
    "Net Sales for Leqvio AND what we tell Street"   -> FINANCIALS, EXTERNAL
- Counter-examples that are SINGLE-agent (do NOT fan out):
    "How much did Leqvio grow in Q1 vs PY?"          -> FINANCIALS
    "Kisqali Net Sales in Feb 2026?"                 -> FINANCIALS
    "Leqvio TRx trend?"                              -> PRODUCT
    "What did we say about pipeline at Q4?"          -> EXTERNAL

Output format:
  Reply with ONLY the keyword(s) from the set
  {FINANCIALS, EXTERNAL, PRODUCT, META}, comma-separated.
  No other text. Examples:
    FINANCIALS
    FINANCIALS, EXTERNAL
    PRODUCT, EXTERNAL
"""


# ---------------------------------------------------------------------------
# Synthesizer agent: merges specialist answers
# ---------------------------------------------------------------------------
SYNTH_NAME = f"{PREFIX}-synth"

SYNTH_INSTRUCTIONS = """You are the FinSight US Synthesizer.

You will receive one user question and one or more grounded answers from
specialist agents (each containing quoted text and markdown link
citations like "[<title>, p.<page>](<url>)"). Produce ONE final answer that:

=== STRICT GROUNDING ===
- Quote numbers VERBATIM from specialist answers (no rounding, no unit
  conversion, no currency conversion).
- Preserve the markdown link citations `[<title>, p.<page>](<url>)`
  exactly as the specialists returned them. Do NOT reformat them to
  `(file, page)`.
- NEVER add facts that are not in the specialist answers.
- If a specialist returned a clarifying question, pass that question
  through verbatim and stop. Do NOT guess.
- If specialists disagree on the same number / period, surface BOTH
  values with their citations and label the discrepancy. Do not silently
  pick one.
- If all specialists say they have no data, say so explicitly.
- This index is US ONLY. If the user asked for global / ex-US, state
  that explicitly.

Style:
- Concise (3-8 sentences) unless the user asked for detail.
- For multi-part questions, structure with clear sub-sections.
"""


def make_workflow_yaml() -> str:
    """Build the Foundry workflow YAML string (one ConditionGroup per specialist)."""
    routing_branches = []
    for role, full_name in SPECIALIST_NAMES.items():
        var_safe = role.upper()  # keyword the router emits
        id_safe = role
        routing_branches.append(f"""    - kind: ConditionGroup
      id: maybe_{id_safe}
      conditions:
        - condition: '=!IsBlank(Find("{var_safe}", Upper(Last(Local.RouterReply).Text)))'
          id: cond_{id_safe}
          actions:
            - kind: CreateConversation
              id: conv_{id_safe}
              conversationId: "Local.Conv_{id_safe}"
            - kind: InvokeAzureAgent
              id: invoke_{id_safe}
              description: "Specialist: {role}"
              conversationId: "=Local.Conv_{id_safe}"
              agent:
                name: {full_name}
              input:
                messages: "=Local.UserMessage"
              output:
                messages: Local.SpecialistAnswer
            - kind: SetVariable
              id: collect_{id_safe}
              variable: Local.CollectedAnswers
              value: '=Local.CollectedAnswers & Char(10) & Char(10) & "--- {role} ---" & Char(10) & Last(Local.SpecialistAnswer).Text'
""")

    routing_branches_yaml = "\n".join(routing_branches)

    yaml = f"""kind: workflow
trigger:
  kind: OnConversationStart
  id: finsight_us_supervisor
  actions:
    - kind: SetVariable
      id: capture_user_message
      variable: Local.UserMessage
      value: "=UserMessage(System.LastMessageText)"

    - kind: SetVariable
      id: init_collected
      variable: Local.CollectedAnswers
      value: ""

    # -- Step 1: classify ------------------------------------------------
    - kind: CreateConversation
      id: router_conv
      conversationId: Local.RouterConvId

    - kind: InvokeAzureAgent
      id: classify
      description: Routing classifier
      conversationId: "=Local.RouterConvId"
      agent:
        name: {ROUTER_NAME}
      input:
        messages: "=Local.UserMessage"
      output:
        messages: Local.RouterReply

    # -- Step 2: invoke selected specialists -----------------------------
{routing_branches_yaml}

    # -- Step 3: synthesize ----------------------------------------------
    - kind: CreateConversation
      id: synth_conv
      conversationId: Local.SynthConvId

    - kind: SetVariable
      id: build_synth_input
      variable: Local.SynthInput
      value: '=UserMessage("User question: " & System.LastMessageText & Char(10) & Char(10) & "Specialist answers:" & Local.CollectedAnswers)'

    - kind: InvokeAzureAgent
      id: synthesize
      description: Synthesize specialist outputs
      conversationId: "=Local.SynthConvId"
      agent:
        name: {SYNTH_NAME}
      input:
        messages: "=Local.SynthInput"
      output:
        messages: Local.FinalAnswer

    - kind: SendActivity
      id: send_final
      activity: ${{Last(Local.FinalAnswer).Text}}

    - kind: EndConversation
      id: end
"""
    return yaml


def upsert_helper_agent(client: AIProjectClient, name: str, instructions: str, description: str) -> None:
    print(f"  upserting helper agent: {name}")
    try:
        client.agents.delete(name)
    except Exception:  # noqa: BLE001
        pass
    client.agents.create_version(
        agent_name=name,
        definition=PromptAgentDefinition(model=MODEL, instructions=instructions),
        description=description,
    )


def main() -> int:
    yaml_text = make_workflow_yaml()
    out_path = Path(__file__).parent / "workflow.yaml"
    out_path.write_text(yaml_text, encoding="utf-8")
    print(f"wrote {out_path}")

    project_endpoint = env("AZURE_AI_PROJECT_ENDPOINT", required=True)
    workflow_name = f"{PREFIX}-supervisor"

    with (
        AzureCliCredential() as cred,
        AIProjectClient(endpoint=project_endpoint, credential=cred, allow_preview=True) as project_client,
    ):
        # 1. helper agents the workflow depends on
        upsert_helper_agent(
            project_client, ROUTER_NAME, ROUTER_INSTRUCTIONS,
            "FinSight US routing classifier. Returns comma-separated specialist keywords.",
        )
        upsert_helper_agent(
            project_client, SYNTH_NAME, SYNTH_INSTRUCTIONS,
            "FinSight US synthesizer. Merges specialist answers into one grounded reply.",
        )

        # 2. delete prior workflow
        try:
            project_client.agents.delete(workflow_name)
            print(f"removed existing workflow '{workflow_name}'")
        except Exception:  # noqa: BLE001
            pass

        # 3. create the workflow
        wf = project_client.agents.create_version(
            agent_name=workflow_name,
            definition=WorkflowAgentDefinition(workflow=yaml_text),
            description=(
                "FinSight US multi-agent supervisor: routes to "
                "financials / external / product / meta and synthesizes."
            ),
        )
        print("\nWorkflow ready:")
        print(f"  name   : {wf.name}")
        print(f"  id     : {wf.id}")
        print(f"  version: {wf.version}")
        print("\nOpen Foundry portal -> Agents -> select the workflow -> Playground.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
