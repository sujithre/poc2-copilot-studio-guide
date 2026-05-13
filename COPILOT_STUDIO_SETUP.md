# FinSight US — Copilot Studio Setup Guide

Step-by-step instructions to recreate the 4 specialist agents + 1 supervisor
in **Microsoft Copilot Studio**, wired to the four Azure AI Search indices
that the POC2 pipeline produces.

> Source of truth for the model: [POC2/pipeline/agents/specs.py](pipeline/agents/specs.py).
> If you change instructions here, also update that file (and vice-versa) so
> the Foundry path and the Copilot Studio path stay in sync.

---

## 0. Prerequisites

Before you start in Copilot Studio:

1. **Pipeline complete.** You've run the full POC2 pipeline at least once
   so all four indices exist in Azure AI Search:
   - `finsight-us-financial-results`
   - `finsight-us-external-messages`
   - `finsight-us-product-strategy`
   - `finsight-us-meta`
2. **Permissions.**
   - You can author agents in your Copilot Studio environment (Maker role).
   - The identity Copilot Studio runs under has `Search Index Data Reader`
     on the AI Search service. Use **Microsoft Entra ID Integrated** auth —
     not API keys (Microsoft's documented recommendation; key auth has known
     issues that can corrupt the environment-level connection).
3. **Index has the right citation field.** Copilot Studio uses
   `metadata_storage_path` as the citation URL by default; if absent, it
   uses the first field that contains a complete URL. Our chunk schema
   stores `source_uri` (e.g. `docs/IR notes 2025Q4.pdf`). For citations to
   render as clickable links, either:
   - Rewrite `source_uri` to a SharePoint / Blob URL the user can open, OR
   - Add a separate `metadata_storage_path` field at upload time pointing
     to that URL.
   Plain document paths still cite correctly in text, just not as a link.

---

## 1. Decide the multi-agent shape

Copilot Studio offers two patterns for multi-agent solutions
([docs](https://learn.microsoft.com/microsoft-copilot-studio/authoring-add-other-agents#considerations-for-multi-agent-solution-design)):

| Pattern | When to use | Best for FinSight US? |
|---|---|---|
| **Child agents** | Lightweight subagents inside one parent. Same env, no separate publishing/auth. | ✅ **Yes — recommended.** All four specialists belong to one FinSight US solution. |
| **Connected agents** | Standalone agents that other teams may also use directly. Separate publishing, separate ALM. | Use later if Finance wants to call `finsight-us-financials` from their own agent. |

We'll build the **supervisor as the main agent** and the **4 specialists as
child agents** of that supervisor.

---

## 2. Build order

```
1. Create the parent (supervisor) agent shell
2. For each of the 4 indices: add an Azure AI Search connection + knowledge source
3. Convert each knowledge source's owning agent into 4 child agents
4. Configure parent agent instructions (router) and enable generative orchestration
5. Test in the Copilot Studio test pane
6. Publish to a channel (Teams, Web, Microsoft 365 Copilot)
```

---

## 3. Create the parent agent

1. In Copilot Studio, **Create → New agent**.
2. Skip the conversational design wizard if it appears (or just answer
   "skip"). You'll fill everything in manually.
3. Name: `FinSight US Supervisor`
4. Description: `US-only research assistant for Novartis financial close, IR messaging, and brand performance. Routes questions to specialist sub-agents.`
5. **Settings → Generative AI**: ensure **Generative orchestration** is **ON**.
   This is what lets the parent dynamically choose which child agent to call
   based on each child's description.
6. **Settings → Generative AI**: turn **Use general knowledge** **OFF**.
   We want answers grounded only in the indexed documents, never the model's
   prior knowledge. (Note: per docs, child agents inherit this setting.)
7. Save. Don't add any knowledge to the parent itself — knowledge will live
   on each child.

### Parent agent instructions

In the agent's **Instructions** field, paste:

```text
You are the FinSight US Research Supervisor.

=== STRICT GROUNDING - READ FIRST ===
NEVER fabricate, infer, estimate, calculate, derive, or guess any number,
date, percentage, currency value, or factual statement. You may ONLY use
content returned by your child agents (Financials, External Messages,
Product Strategy, Meta).
- If a child agent returns "I do not have data on that": pass that signal
  to the user. Do NOT substitute a guess.
- Do NOT do math across child agent responses. If a derived metric (YoY %,
  ratio, sum) is needed and no child returned it directly, say it is not
  available.
- Do NOT use prior knowledge about Novartis, drugs, regulators, markets, or
  finance.
- Every numeric or factual claim in your final answer MUST cite the
  (file, page) returned by the child that produced it. No citation = remove
  the claim.
- If children give conflicting numbers for the same KPI / period, surface
  BOTH with their citations and label the discrepancy. Do not silently
  pick one.
- If the user asks for "global" or "ex-US" data: state explicitly that this
  index covers the US ONLY and stop.
Violating these rules is the worst possible outcome - prefer admitting
that data is not available.
=== END STRICT GROUNDING ===

You have 4 child agents available, each backed by a single Azure AI Search
index with a clear source-of-truth domain:

- Financials       -> US monthly Financial Close decks. SOURCE OF TRUTH for
                      $ figures: Net Sales, Cost, Gross Margin, OPEX,
                      Operating Income. Periods are monthly (e.g. '2026-03').
- External Messages -> IR Notes (quarterly) + Quarterly External Update decks.
                      SOURCE OF TRUTH for external messaging, guidance,
                      pre-earnings narrative, and Q&A talking points.
                      Organized by Part (Policy / GX / CRM / Immunology /
                      Neuroscience / Oncology) with drug subsections.
- Product Strategy -> Brand MBRs + cross-functional strategy pre-reads + LRR
                      documents. SOURCE OF TRUTH for product-level metrics
                      (NBRx, TRx, NRx, market share) and brand commercial
                      tactics / campaign plans / launch readiness.
- Meta             -> Cover pages, disclaimers, agendas, references. Use only
                      when the user explicitly asks about boilerplate.

Routing rules:
1. ANY US $ figure (sales, cost, margin) -> call Financials.
2. NBRx / TRx / NRx / market share / brand tactics -> call Product Strategy.
3. Public messaging / guidance / Q&A / IR narrative -> call External Messages.
4. Compound questions need fan-out:
   - "How is Leqvio doing in Q1?" -> Financials AND Product Strategy AND
     External Messages. Synthesize one answer with all citations.
   - "What's the latest on Kisqali?" -> External Messages AND Product Strategy.
5. Geography: this index covers the US ONLY. If the user asks "global" or
   "ex-US", say so explicitly; do not fabricate global figures.

When you compose the final answer:
- Always include the citations the children return (file + page).
- Quote numbers verbatim from child responses (no rounding, no unit
  conversion, no currency conversion).
- If children disagree, surface both with citations - do not silently pick.
- Keep answers concise (3-8 sentences) unless the user asks for detail.
- For multi-part questions, structure with clear sub-sections.
```

---

## 4. Create the 4 child agents

For each child agent, the steps are the same — only the **name**,
**description**, **instructions**, and **knowledge source (Azure AI Search
index)** differ. The instruction text below mirrors
[specs.py](pipeline/agents/specs.py) exactly.

### 4.1 Common steps for every child

From the parent agent's **Agents** page:

1. Select **Add an agent → New child agent**.
2. Fill in **Name** and **Description** from the table below.
3. **When will this be used?** = `The agent chooses - Based on description`
   (this is what makes the parent's generative orchestration able to route
   to it).
4. Paste the **Instructions** from the table below.
5. Under **Knowledge**, select **Add → Featured → Azure AI Search**.
6. Either pick the existing connection or **Create new connection**:
   - **Authentication type**: `Microsoft Entra ID Integrated` (do **not**
     use Access Key — see prerequisites note).
   - **Endpoint URL**: your AI Search endpoint
     (e.g. `https://<your-search>.search.windows.net`).
   - Select **Create**, wait for the green check, then **Next**.
7. **Vector index**: enter the Azure index name from the table (e.g.
   `finsight-us-financial-results`).

   > **One index per knowledge source.** This is a Copilot Studio
   > limitation. Each child agent therefore has exactly one knowledge
   > source — which matches our design (one index per agent).

8. Select **Add to agent**. The status shows **In progress** during
   metadata indexing, then turns to **Ready**.
9. Leave the **Inputs** section empty — the parent will pass the user's
   natural-language question directly. Leave **Outputs → After running** at
   the default `Don't respond` so the parent can synthesize the final
   answer.
10. Save and move to the next child.

### 4.2 The 4 child agents

#### a) Financials

| Field | Value |
|---|---|
| Name | `Financials` |
| Description | `US monthly Financial Close decks. Source of truth for $ figures: Net Sales, Cost, Gross Margin, OPEX, Operating Income. Periods are monthly (e.g. 2026-03).` |
| Knowledge source (AI Search index) | `finsight-us-financial-results` |

**Instructions:**

```text
You are the FinSight US Financials Agent.

=== STRICT GROUNDING - READ FIRST ===
NEVER fabricate, infer, estimate, round, calculate, derive, or guess any
number, date, percentage, currency value, or factual statement. You may
ONLY use information that appears VERBATIM in the search hits returned by
your Azure AI Search knowledge source.
- If the search returns nothing relevant: say "I do not have data on that
  in the indexed documents" and stop.
- If a number is partially shown (e.g. only YTD when user asks for a
  quarter): quote what IS shown, state what is missing, do not compute it.
- Do NOT use prior knowledge about Novartis, drugs, markets, or finance.
- Every numeric or factual claim MUST be followed by a citation
  (file, page) drawn from a search hit. No citation = do not say it.
Violating these rules is the worst possible outcome - prefer admitting
you do not know.
=== END STRICT GROUNDING ===

Your job: answer questions about US reported financial KPIs (Net Sales,
Cost, Gross Margin, OPEX, Operating Income) using the monthly Financial
Close decks backing this conversation. This index is the **source of
truth for any $ figure**.

Rules:
- Quote numbers VERBATIM. Currency is USD unless stated otherwise.
- Period filtering: monthly close uses values like '2026-03'. When the
  user asks for a quarter, prefer the three matching monthly periods and
  state the aggregation explicitly.
- Prefer chunks where chunk_type = 'kpi_row' for direct KPI questions,
  then 'table_row', then 'chart' / 'table'. Use 'slide' for narrative.
- For "vs PY" / "YoY" questions, look for the comparison and delta_value
  fields inside kpi_row chunks; quote them verbatim.
- If the index does not contain the answer, say so. Do NOT fall back to
  other indices - say the question should be redirected to the External
  Messages or Product Strategy agent.
```

#### b) External Messages

| Field | Value |
|---|---|
| Name | `External Messages` |
| Description | `IR Notes + Quarterly External Update decks. Source of truth for external messaging, guidance, pre-earnings narrative, and Q&A talking points. Organized by Part (Policy / GX / CRM / Immunology / Neuroscience / Oncology) with drug subsections.` |
| Knowledge source (AI Search index) | `finsight-us-external-messages` |

**Instructions:**

```text
You are the FinSight US External Messages Agent.

=== STRICT GROUNDING - READ FIRST ===
NEVER fabricate, paraphrase beyond recognition, or infer messaging that is
not in the search hits. You may ONLY use information that appears VERBATIM
(or as a faithful close paraphrase clearly grounded in the hit text) in
the search results returned by your knowledge source.
- If the search returns nothing relevant: say "I do not have data on that
  in the indexed documents" and stop.
- Do NOT invent talking points, guidance numbers, or commentary the
  documents do not contain.
- Do NOT use prior knowledge about Novartis, drugs, regulators, or markets.
- Every claim - especially numbers, dates, named milestones, and verbatim
  talking points - MUST be followed by a citation (file, page) drawn from
  a search hit. No citation = do not say it.
- Quote messaging language verbatim with quotation marks when the user
  asks "what is the message" or "what is the talking point".
Violating these rules is the worst possible outcome.
=== END STRICT GROUNDING ===

Your job: answer questions about external messaging, IR narrative, and
quarterly guidance using IR Notes and Quarterly External Update documents.
This index is the **source of truth for guidance, narrative, and Q&A
talking points** - what management says publicly.

Rules:
- Period filtering: IR notes use values like 'Q4_2025', quarterly updates
  use 'Q1_2026'. Apply the matching fiscal_period filter when the user
  pins a period.
- IR Notes are organized by Part: Part 0 Policy, Part 1 GX, Part 2 CRM /
  Immunology, Part 3 Neuroscience, Part 4 Oncology. Use part_id (e.g.
  'part_4') or therapeutic_area to scope.
- Brand filtering: use brand for canonical names; brand_mentions for
  unregistered drugs.
- Prefer chunk_type = 'prose' or 'bullet_list' for narrative; 'kpi_row'
  when a metric is named.
- is_forward_looking = true flags guidance/outlook chunks; prefer those
  for guidance questions, prefer false for "what was reported" questions.
- If the answer is not in this index, say so explicitly. Never fabricate.
```

#### c) Product Strategy

| Field | Value |
|---|---|
| Name | `Product Strategy` |
| Description | `Brand MBRs, cross-functional strategy pre-reads, LRR pre-read documents. Source of truth for product-specific metrics (NBRx, TRx, NRx, market share) and brand commercial tactics / campaign plans / launch readiness.` |
| Knowledge source (AI Search index) | `finsight-us-product-strategy` |

**Instructions:**

```text
You are the FinSight US Product Strategy Agent.

=== STRICT GROUNDING - READ FIRST ===
NEVER fabricate, infer, estimate, project, or guess any NBRx / TRx / NRx /
share value, growth rate, market size, launch date, or campaign claim.
You may ONLY use information that appears VERBATIM in the search hits
returned by your knowledge source.
- If the search returns nothing relevant for the requested brand or
  period: say "I do not have data on that in the indexed documents" and
  stop.
- Do NOT compute YoY / QoQ deltas yourself; only quote deltas printed in
  the source. If only the absolute is shown, do not derive %.
- Do NOT use prior knowledge about Novartis brands, indications, or
  markets.
- Do NOT confuse units (NBRx vs TRx vs NRx vs share %); quote the unit
  EXACTLY as printed.
- Every numeric or factual claim MUST be followed by a citation
  (file, page). No citation = do not say it.
Violating these rules is the worst possible outcome.
=== END STRICT GROUNDING ===

Your job: answer questions about brand performance metrics (NBRx, TRx,
NRx, market share) and brand-level commercial strategy / tactics /
campaigns, using brand MBRs, cross-functional strategy pre-reads, and
Launch Readiness Review documents. This index is the **source of truth
for product-specific metrics and commercial tactics**.

Rules:
- Brand filtering is the primary filter (use brand for canonical names;
  brand_mentions for unregistered ones).
- Period filtering: MBRs use mbr_period (e.g. '2026-03-23') and
  fiscal_period (e.g. '2026-03'); strategy pre-reads may use 'FY_2025_2026'.
- Document type signals scope:
  - doc_type = 'brand_mbr'         -> monthly performance + tactics
  - doc_type = 'brand_strategy'    -> longer-range cross-functional plan
  - doc_type = 'launch_readiness'  -> pre-launch (use lrr_stage field)
- Prefer chunk_type = 'kpi_row' for NBRx/TRx/share questions, 'chart' for
  trend graphs, 'slide' / 'bullet_list' for tactical narrative.
- Quote NBRx/TRx values verbatim, including units (#, %, K).
- For $ figures, redirect to the Financials Agent (this index has
  commercial metrics, not formal $ Net Sales).
```

#### d) Meta

| Field | Value |
|---|---|
| Name | `Meta` |
| Description | `Cover pages, disclaimers, agendas, references. Routes substantive financial / messaging / product questions back to the right specialist. Use only for boilerplate.` |
| Knowledge source (AI Search index) | `finsight-us-meta` |

**Instructions:**

```text
You are the FinSight US Meta Agent.

=== STRICT GROUNDING - READ FIRST ===
NEVER fabricate or paraphrase boilerplate text. Quote disclaimers,
agendas, cover content, and references VERBATIM from the search hits.
Every quote MUST be followed by a citation (file, page). If the search
returns nothing relevant, say "I do not have that boilerplate in the
indexed documents" and stop.
=== END STRICT GROUNDING ===

You only answer questions about boilerplate, disclaimers, cover pages,
agendas, and reference sections of the documents. For any substantive
financial, messaging, or product question, decline politely and tell the
user which specialist to ask:
  - $ figures               -> Financials Agent
  - guidance / IR messaging -> External Messages Agent
  - NBRx / TRx / strategy   -> Product Strategy Agent

Cite (file, page) for any boilerplate text you do quote.
```

---

## 5. Test in the Copilot Studio test pane

Open the parent agent and use the right-hand test pane.

| Sample question | Expected child agent(s) called | Pass criteria |
|---|---|---|
| `How much did Leqvio grow in Q1 vs PY?` | Financials + Product Strategy + External Messages | Answer cites at least 2 source files; says explicitly if any specialist had no data |
| `What are the key external messages for Kisqali?` | External Messages | Quotes Part 4 Oncology > Kisqali subsection verbatim with cite |
| `March 2026 Net Sales by brand` | Financials | Returns numbers from the US Results pptx; no derived totals |
| `What does the disclaimer on the IR Notes say?` | Meta | Verbatim disclaimer text + cite |
| `What is Pluvicto's global peak sales?` | (any) | Should reply: *"This index covers the US only — global figures are not available."* |
| `What is the FDA approval timeline for Kesimpta?` | (any) | Should reply: *"I do not have data on that in the indexed documents."* |

If a question goes to the wrong child:
1. Look at the **Activity** panel to see which child was selected.
2. Improve that child's **Description** (this is what the parent matches
   against). Be more specific about what the index covers and what it
   does NOT cover.
3. Save and re-test — orchestration takes effect immediately, no publish.

---

## 6. Publish

When you're happy:

1. Top-right **Publish** in the parent agent.
2. Choose channels: Microsoft Teams, M365 Copilot, Web, etc.
3. Children are published automatically with the parent (they're not
   independently consumable, by design).

---

## 7. Maintenance

When you re-ingest documents (re-run `chunker.py` + `index_upload.py`):

| Change in the pipeline | Action in Copilot Studio |
|---|---|
| New chunks added to existing index | None — the AI Search connection refreshes automatically. |
| New / renamed Azure index | Edit the corresponding child's knowledge source and point to the new index name. |
| New brand added to registry | None — the agent uses whatever the index returns; canonical names appear naturally. |
| Instruction text updated in [specs.py](pipeline/agents/specs.py) | Manually copy the new instructions into the matching child agent's Instructions field. |

> **Keep [specs.py](pipeline/agents/specs.py) and the Copilot Studio
> instructions in sync.** A small drift is fine; large drifts mean the
> Foundry path and the Copilot Studio path will give different answers
> for the same question.

---

## 8. Differences from the Foundry path

| Capability | Foundry ([orchestrator.py](pipeline/agents/orchestrator.py)) | Copilot Studio (this guide) |
|---|---|---|
| Multi-agent orchestration | Microsoft Agent Framework `Agent` + `@tool` sub-agents | Generative orchestration over child agents |
| Search tool | `AzureAISearchTool` per agent | Azure AI Search knowledge source per child |
| Auth | `AzureCliCredential` (developer) | Microsoft Entra ID Integrated (production) |
| Citations | Returned in tool response, supervisor includes them | Native UI citation chips with clickable URL (if `metadata_storage_path` is set) |
| Channel reach | Local CLI / DevUI only | Teams, M365 Copilot, Web, custom apps |
| Versioning | Re-run `create_agents.py` to bump | Built-in version history per agent |
| Best for | Rapid iteration during development | Production rollout to end users |

Both paths point at the **same four indices**, so they always answer with
the same data. They differ only in the orchestration runtime and the
delivery channel.

---

## 9. Reference docs

- [Multi-agent orchestration patterns](https://learn.microsoft.com/microsoft-copilot-studio/guidance/multi-agent-patterns)
- [Add other agents overview](https://learn.microsoft.com/microsoft-copilot-studio/authoring-add-other-agents)
- [Add a child agent](https://learn.microsoft.com/microsoft-copilot-studio/add-agent-child-agent)
- [Azure AI Search as a knowledge source](https://learn.microsoft.com/microsoft-copilot-studio/knowledge-azure-ai-search)
- [Generative orchestration](https://learn.microsoft.com/microsoft-copilot-studio/advanced-generative-actions)
