"""Multi-agent orchestrator over the FinSight US Foundry agents.

Supervisor (Microsoft Agent Framework) with `@tool` sub-agents, each calling
a Foundry portal agent via the Responses API.

Run:
    python orchestrator.py "How much did Leqvio grow in Q1 vs PY?"
    python orchestrator.py "What are the key external messages for Kisqali?"
    python orchestrator.py --devui     # open the local DevUI to chat

Auth:
    Run `az login` first. Both the supervisor's chat client and the
    Responses-API calls use AzureCliCredential.

Env (.env auto-loaded from POC2/.env or workspace .env):
    AZURE_OPENAI_ENDPOINT             Azure OpenAI / Foundry chat endpoint
    AZURE_OPENAI_API_VERSION          e.g. 2024-10-21
    AZURE_OPENAI_VISION_DEPLOYMENT    Chat model used by the supervisor (gpt-4.1 etc.)
    AZURE_AI_PROJECT_ENDPOINT         Foundry project endpoint - calls specialists
    FOUNDRY_AGENT_PREFIX              (default 'finsight-us')
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Annotated

# Sibling pipeline modules importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_framework import Agent, tool
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient
from pydantic import Field
from rich import print as rprint
from rich.logging import RichHandler

from common import env

logging.basicConfig(level=logging.WARNING, handlers=[RichHandler(show_path=False)], force=True)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# One Foundry Responses-API client, shared by every sub-agent tool.
# ---------------------------------------------------------------------------
def _foundry_responses_client():
    project_endpoint = env("AZURE_AI_PROJECT_ENDPOINT", required=True)
    proj = AIProjectClient(endpoint=project_endpoint, credential=AzureCliCredential())
    try:
        return proj.get_openai_client()
    except TypeError:
        return proj.get_openai_client(api_version="2025-04-01-preview")


_RESPONSES_CLIENT = None


def _client():
    global _RESPONSES_CLIENT
    if _RESPONSES_CLIENT is None:
        _RESPONSES_CLIENT = _foundry_responses_client()
    return _RESPONSES_CLIENT


def _agent_full_name(role: str) -> str:
    prefix = env("FOUNDRY_AGENT_PREFIX", "finsight-us")
    return f"{prefix}-{role}"


def _ask_specialist(role: str, query: str) -> str:
    """Call a Foundry portal agent via the Responses API and return its text."""
    name = _agent_full_name(role)
    logger.info(f"-> {name}  {query!r}")
    oc = _client()
    conv = oc.conversations.create(
        items=[{"type": "message", "role": "user", "content": query}]
    )
    try:
        resp = oc.responses.create(
            conversation=conv.id,
            extra_body={"agent_reference": {"name": name, "type": "agent_reference"}},
        )
        text = getattr(resp, "output_text", None)
        if text:
            return text
        parts = []
        for item in getattr(resp, "output", []) or []:
            for c in getattr(item, "content", []) or []:
                t = getattr(c, "text", None)
                if t:
                    parts.append(t)
        return "\n".join(parts) or "(no response)"
    finally:
        try:
            oc.conversations.delete(conversation_id=conv.id)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Sub-agent tools (one per specialist). The supervisor decides which to call.
# ---------------------------------------------------------------------------

@tool
def ask_financials(
    query: Annotated[str, Field(description="A US $ KPI question (Net Sales, Cost, Gross Margin, OPEX, Operating Income) for a specific period.")],
) -> str:
    """Ask the US monthly Financial Close specialist. Source of truth for $ figures."""
    return _ask_specialist("financials", query)


@tool
def ask_external(
    query: Annotated[str, Field(description="A question about IR notes / quarterly external updates: messaging, guidance, Q&A talking points, period-on-period commentary by brand or therapeutic area.")],
) -> str:
    """Ask the External Messages specialist (IR Notes + Quarterly Updates)."""
    return _ask_specialist("external", query)


@tool
def ask_product(
    query: Annotated[str, Field(description="A brand-level question about NBRx, TRx, NRx, market share, or commercial tactics / campaigns / launch readiness for a specific brand.")],
) -> str:
    """Ask the Product Strategy specialist (MBRs + Strategy + LRR pre-reads)."""
    return _ask_specialist("product", query)


@tool
def ask_meta(
    query: Annotated[str, Field(description="A question about boilerplate, disclaimers, cover pages, agendas, or references.")],
) -> str:
    """Ask the Meta agent (covers/disclaimers/agendas/references). Rarely needed."""
    return _ask_specialist("meta", query)


SPECIALIST_TOOLS = [
    ask_financials,
    ask_external,
    ask_product,
    ask_meta,
]


SUPERVISOR_INSTRUCTIONS = """You are the FinSight US Research Supervisor.

=== STRICT GROUNDING - READ FIRST ===
NEVER fabricate, infer, estimate, calculate, derive, or guess any number,
date, percentage, currency value, or factual statement. You may ONLY use
content returned by your specialist tools (ask_financials, ask_external,
ask_product, ask_meta).
- If a specialist returns "I do not have data on that": pass that signal to
  the user. Do NOT substitute a guess.
- Do NOT carry numbers from one tool response to another to do math on
  them. If a derived metric (YoY %, ratio, sum) is needed and no
  specialist returned it directly, say it is not available.
- Do NOT use prior knowledge about Novartis, drugs, regulators, markets,
  or finance.
- Every numeric or factual claim in your final answer MUST cite the
  `(file, page)` returned by the specialist that produced it. No
  citation = remove the claim.
- If specialists give conflicting numbers for the same KPI / period,
  surface BOTH with their citations and label the discrepancy. Do not
  silently pick one.
- If the user asks for "global" or "ex-US" data: state explicitly that
  this index covers the US ONLY and stop.
Violating these rules is the worst possible outcome - prefer admitting the
data is not available.
=== END STRICT GROUNDING ===

You have 4 specialist agents available as tools, each backed by a single
Azure AI Search index. Each index has a clear source-of-truth domain:

- ask_financials  -> US monthly Financial Close decks. SOURCE OF TRUTH for any
                     $ figure: Net Sales, Cost, Gross Margin, OPEX,
                     Operating Income. Periods are monthly (e.g. '2026-03').
- ask_external    -> IR Notes (quarterly) + Quarterly External Update decks.
                     SOURCE OF TRUTH for external messaging, guidance,
                     pre-earnings narrative, and Q&A talking points.
                     Organized by Part (Policy / GX / CRM / Immunology /
                     Neuroscience / Oncology) with drug subsections.
- ask_product     -> Brand MBRs + cross-functional strategy pre-reads + LRR
                     documents. SOURCE OF TRUTH for product-level metrics
                     (NBRx, TRx, NRx, market share) and brand commercial
                     tactics / campaign plans / launch readiness.
- ask_meta        -> Cover pages, disclaimers, agendas, references. Use only
                     when the user explicitly asks about boilerplate.

Routing rules:
1. ANY US $ figure (sales, cost, margin) -> ask_financials.
2. NBRx / TRx / NRx / market share / brand tactics -> ask_product.
3. Public messaging / guidance / Q&A / IR narrative -> ask_external.
4. Compound questions need fan-out. Examples:
     - "How is Leqvio doing in Q1?" -> ask_financials ($) AND ask_product (NBRx)
       AND ask_external (messaging). Synthesize one answer with all citations.
     - "What's the latest on Kisqali?" -> ask_external (narrative) AND
       ask_product (metrics).
5. Geography: this index covers the US ONLY. If the user asks for "global" or
   "ex-US" data, say so explicitly; do not fabricate global figures.
6. If the user names a drug not registered as a known brand, the specialists
   will still try `brand_mentions` filtering. If they say no data, accept it.

When you compose the final answer:
- Always include the citations the specialists return (file + page).
- Quote numbers verbatim from the specialist responses (no rounding,
  no unit conversion, no currency conversion).
- If specialists disagree on a number, surface the discrepancy with both
  citations - do not silently pick one.
- Keep answers concise (3-8 sentences) unless the user asks for detail.
- For multi-part questions, structure the answer with clear sub-sections.
"""


def make_supervisor() -> Agent:
    aoai_endpoint = env("AZURE_OPENAI_ENDPOINT", required=True)
    deployment = env("AZURE_OPENAI_VISION_DEPLOYMENT", required=True)
    cred = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(cred, "https://cognitiveservices.azure.com/.default")
    chat_client = OpenAIChatClient(
        base_url=f"{aoai_endpoint.rstrip('/')}/openai/v1/",
        api_key=token_provider,
        model=deployment,
    )
    return Agent(
        name="finsight-us-supervisor",
        client=chat_client,
        instructions=SUPERVISOR_INSTRUCTIONS,
        tools=SPECIALIST_TOOLS,
    )


async def run_query(question: str) -> None:
    supervisor = make_supervisor()
    rprint(f"\n[bold]User:[/bold] {question}\n")
    response = await supervisor.run(question)
    rprint("[bold green]Supervisor:[/bold green]")
    rprint(response.text)


def main() -> int:
    if "--devui" in sys.argv:
        from agent_framework.devui import serve  # type: ignore[import-not-found]
        serve(entities=[make_supervisor()], auto_open=True)
        return 0
    if len(sys.argv) < 2:
        print("usage: python orchestrator.py \"<question>\"   (or --devui)", file=sys.stderr)
        return 2
    question = " ".join(sys.argv[1:])
    asyncio.run(run_query(question))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
