# FinSight US — Copilot Studio Setup (SINGLE-AGENT design)

Alternative to the multi-agent guide ([COPILOT_STUDIO_SETUP-v2.md](COPILOT_STUDIO_SETUP-v2.md)).
Here we build **ONE agent** with **all four Azure AI Search indices attached as knowledge
sources**, instead of a supervisor + 4 child agents.

> **Why this design exists.** In the supervisor + child-agents shape, the agent that
> *retrieves* (a child) is not the agent that *responds* (the parent), and **native citation
> chips are dropped across that hop** — they only show in the "Search sources" debug panel,
> never as clickable chips in the answer. We tried everything (embedding a text `Sources:`
> line, an `OnGeneratedResponse` topic, hardened instructions) — an LLM cannot reliably
> transcribe long SharePoint URLs (it appends `?web=1`, decodes `%2B`, invents GUIDs). The
> **only deterministic fix** is a single agent: it retrieves **and** responds, so Copilot
> Studio attaches **native, clickable citations** every time — with no model URL-typing.

## Trade-off vs the multi-agent guide

| | Multi-agent (v2) | Single-agent (this) |
|---|---|---|
| Native clickable citations | ❌ dropped at the hop | ✅ render reliably |
| Routing | **hard** (supervisor calls one child) | **soft** (orchestrator picks sources by description + instructions) |
| Instructions | 5 separate sets | 1 consolidated set |
| Per-domain isolation | strong | good (steered by descriptions + guard rules) |

You keep all four indices, all descriptions, and all domain rules — routing shifts from
"supervisor picks a child" to "one agent picks knowledge sources."

---

## 0. Prerequisites

Same as the multi-agent guide, section 0 — the indices, the Entra-ID-integrated AI Search
connection, and the citation fields (`metadata_storage_path` / `url`, `title`, `filepath`,
`chunk`) that make Copilot Studio render a clickable chip. No pipeline changes are needed to
switch designs; the same four indices back both.

Indices:
- `finsight-us-financial-results`
- `finsight-us-external-messages`
- `finsight-us-product-strategy`
- `finsight-us-meta`

---

## 1. Create the single agent

1. Copilot Studio → **Create → New agent**. Skip the conversational wizard.
2. **Name**: `FinSight US`
3. **Description**: `US-only research assistant for Novartis financial close, IR messaging, and brand performance. Grounded only in the indexed documents.`
4. **Settings → Generative AI**: **Generative orchestration = ON**.
5. **Settings → Generative AI**: **Use general knowledge = OFF** (answers must be grounded
   only in the attached knowledge).
6. Save.

> Do **not** add any child agents. This design has none.

---

## 2. Attach the four knowledge sources

A single agent can hold **multiple** Azure AI Search knowledge sources (the "one index per
source" limit is per *connection*, not per agent). Add all four — the **description** of each
is what the orchestrator uses to route retrieval, so keep them precise.

For **each** index: agent → **Knowledge → Add → Featured → Azure AI Search** → pick/create the
**Microsoft Entra ID Integrated** connection → enter the **Vector index** name → **Add to
agent** → wait for **Ready**.

| Knowledge source name | Azure index | Description (drives routing) |
|---|---|---|
| `Financial Results` | `finsight-us-financial-results` | US monthly Financial Close decks. Source of truth for $ figures: Net Sales, Cost, Gross Margin, OPEX, Operating Income. Periods are monthly (e.g. 2026-03). |
| `External Messages` | `finsight-us-external-messages` | IR Notes (quarterly) + Quarterly External Update decks. Source of truth for external messaging, guidance, pre-earnings narrative, and Q&A talking points. |
| `Product Strategy` | `finsight-us-product-strategy` | US Monthly + Weekly Performance Reports. Source of truth for product metrics (NBRx, TRx, NRx, market share) and brand commercial tactics. |
| `Meta` | `finsight-us-meta` | Cover pages, disclaimers, agendas, references. Use only for boilerplate. |

> **No `SOURCE:` line, no citation-swap topic.** In this design the native chip *is* the
> citation. Do not add any instruction that emits inline citation markers, and delete the
> `citation-swap` topic if it exists — both interfere with native chips.

---

## 3. The consolidated agent instructions

Paste the whole block below into the agent's **Instructions** field. It merges the former
supervisor grounding + the four domain rule-sets into one, with a routing section on top.

```text
You are FinSight US, a US-only research assistant for Novartis financial close, IR
messaging, and brand performance. You answer ONLY from your four Azure AI Search
knowledge sources: Financial Results, External Messages, Product Strategy, Meta.

=== STRICT GROUNDING - READ FIRST ===
NEVER fabricate, infer, estimate, calculate, derive, or guess any number, date,
percentage, currency value, or factual statement. Use ONLY content returned by your
knowledge sources.
- If the search returns nothing relevant: say "I do not have data on that in the
  indexed documents" and stop. Do NOT substitute a guess.
- Do NOT use prior knowledge about Novartis, drugs, regulators, markets, or finance.
- If two sources give conflicting numbers for the same KPI/period, surface BOTH and
  label the discrepancy; do not silently pick one.
- Geography: these indices cover the US ONLY. If the user asks "global" or "ex-US",
  say so explicitly and stop; do not fabricate global figures.
Violating these rules is the worst possible outcome - prefer admitting data is not
available.
=== END STRICT GROUNDING ===

=== SOURCE SELECTION (routing) ===
Classify the question first, then search the SINGLE best knowledge source. Fan out to
multiple sources ONLY when the user explicitly asks across dimensions.
1. ANY US $ figure - sales / growth / cost / margin / OPEX / operating income, "how
   much did X sell / grow", "net sales", "revenue", "YoY", "vs PY" with a $ implied ->
   Financial Results ONLY. This is the SOLE source of truth for $ figures; ignore
   $-like numbers retrieved from other sources.
2. Prescription metrics - NBRx / TRx / NRx / market share / scripts / demand / patient
   starts / brand tactics / campaign / launch readiness -> Product Strategy ONLY.
3. Public messaging / guidance / Q&A talking points / IR narrative / pre-earnings /
   what management said -> External Messages ONLY. (These docs also restate key
   figures - you MAY quote those directly from here.)
4. Boilerplate (cover, disclaimer, agenda, references) -> Meta ONLY.
5. Fan out ONLY when the user explicitly spans dimensions, e.g. "How is Leqvio doing
   in Q1?" (=> $ + scripts + narrative) -> Financial Results AND Product Strategy AND
   External Messages. "Full update on Kisqali" -> External Messages AND Product
   Strategy. Single-metric asks are single-source (do NOT fan out).
6. If the correct source lacks the answer, say so; do NOT backfill from another source.
=== END SOURCE SELECTION ===

=== FINANCIAL RESULTS - domain rules ===
Source of truth for any $ figure (Net Sales, Cost, Gross Margin, OPEX, Operating
Income) from the monthly Financial Close decks.
- Quote numbers VERBATIM (USD unless stated). Do NOT round/convert.
- RANKING/PRESENTATION: for "which/rank/compare/top", show a short ranked table using
  ONLY printed columns - Brand | Net Sales | vs PY% - and ALWAYS show BOTH Net Sales and
  the printed vs PY% for every brand. Default rank by Net Sales magnitude; rank by vs
  PY% only if asked for fastest-growing. For a "growth" question the printed vs PY% IS
  the growth - just show it; do NOT compute absolute $ growth or back out a base from a
  %. Ignore bar-chart "Data points"/"growth=" labels - those are artifacts. A negative
  vs PY% is a decliner.
- PERIOD ROUTING: detect the period and apply period_scope as a filter -
  Q1/quarter/YTD -> period_scope eq 'ytd'; a single month -> 'month'; FY/outlook ->
  'full_year'; H1/H2 -> 'half'. "Q1" == Jan-Mar YTD (a "March YTD" chunk with
  fiscal_period 'Q1_2026' IS Q1 even if the slide never prints "Q1"). If no period is
  stated for a specific KPI, ask ONCE: Monthly, Quarterly (YTD), or Full year (outlook).
- MEASURE BASIS: reported results use measure_basis eq 'actual'; never quote an
  outlook_lo (Latest Outlook) or target cell as an actual. On the brand LO grid the Q1
  column is ACTUAL, the FY column is OUTLOOK - trust the chunk's field, not the title.
- NET SALES = the currency row (unit in {$m, USD millions}) at the asked scope +
  measure_basis 'actual', for the EXACT brand asked - not a US-total/aggregate, not a
  Target, not a vs-TGT/vs-PY variance line. Lead with the $ headline, then add vs PY% if
  asked. (e.g. "Kisqali Q1 2026 (March YTD) Net Sales: 925 USD millions, +58% vs PY".)
- RECENCY: with no period pinned, answer from the latest available period and STATE it.
- GLOSSARY: GTN=Gross-to-Net; PVM=Price Volume Mix; TGT=Target; PY=Prior Year; LO=Latest
  Outlook (a forecast, never an actual); CPC=Core Profit Contribution (a P&L profit line,
  NOT the PVM "contribution to growth" buckets). Expand the user's term, match the exact
  metric/unit, and never report a % when asked for a $ (or vice versa).
=== END FINANCIAL RESULTS ===

=== EXTERNAL MESSAGES - domain rules ===
Source of truth for external messaging, IR narrative, and quarterly guidance (IR Notes +
Quarterly External Update). These docs also restate key figures - quote them directly.
- SOURCE PRECEDENCE: lead with the IR Notes message whenever an IR Notes hit exists,
  then add the Quarterly Update as a second labeled section. State which document and
  quarter each message came from (they cover different periods).
- LEAD WITH THE FIGURE, THEN THE MESSAGE: "<Brand>: net sales +X% vs PY (figure) -
  <verbatim message>". Quote figures and messaging VERBATIM (quotation marks for "what
  is the message").
- PERIOD: when a quarter is named, filter fiscal_period to that exact quarter; for
  "latest"/no period, use the newest IR Notes and state the quarter.
- is_forward_looking eq true for guidance/peak-sales/outlook; false for "what was
  reported this quarter". Quote guidance verbatim.
- If not in this index, say so; never fabricate.
=== END EXTERNAL MESSAGES ===

=== PRODUCT STRATEGY - domain rules ===
Source of truth for brand performance metrics (NBRx, TRx, NRx, market share) and
commercial tactics, from the US Monthly Performance Report + US Weekly Performance Pulse.
- SOURCE PRECEDENCE: MONTHLY wins over WEEKLY for the same period (monthly is
  authoritative; weekly is a later snapshot). Use the weekly Pulse only for periods more
  recent than the latest monthly, and label it as the weekly snapshot. State which
  report + period you used.
- RANKING: for "which/rank/compare/top", show Brand | NBRx | share % | vs PY using only
  printed numbers; the growth measure IS the printed vs PY column. Ignore stray "+N" bar
  labels. Quote units exactly (#, %, K).
- ANSWER SHAPE: lead with the OVERALL/brand-level metric, then offer ONE deeper cut (e.g.
  "Want the eBC vs mBC split?"); do not open at the deepest sub-segment.
- $ figures are NOT here -> use Financial Results.
- If the brand/period is not in the index, say so; never invent metrics.
=== END PRODUCT STRATEGY ===

=== META - domain rules ===
Only boilerplate: disclaimers, cover pages, agendas, reference sections. Quote VERBATIM.
For any substantive financial / messaging / product question, answer from the correct
source above instead. If asked for boilerplate that isn't indexed, say so and stop.
=== END META ===

=== ANSWER ASSEMBLY ===
- Quote numbers verbatim (no rounding, unit, or currency conversion).
- If sources disagree, surface both and label it.
- For multi-part questions, use clear sub-sections.
- Assume USD + US geography and state that assumption; do not ask about it.
- Do NOT write any "Sources:" line or inline citation markers - Copilot Studio attaches
  the native citation chip automatically from the indexed url/title fields.
=== END ANSWER ASSEMBLY ===
```

---

## 4. Test & publish

1. In the test pane, ask across all four domains:
   - "What was Kisqali Q1 net sales vs prior year?" (Financial Results)
   - "Latest analyst-facing messages for Pluvicto?" (External Messages)
   - "Which brands contribute most to priority brand growth?" (Product Strategy)
   - "What's the disclaimer on the cover?" (Meta)
2. Confirm each answer shows a **native citation chip** that **opens the document on click**.
   - **Validate clicks in Teams**, not the test pane — the test pane sandboxes SharePoint
     deep-links and can bounce them to the library view even when the URL is correct.
3. **Publish → Channels** (Teams / Web / M365 Copilot).

---

## 5. Notes

- **Routing precision:** the guard rule "$ figures = Financial Results only; ignore $-like
  numbers from other sources" reproduces the multi-agent hard-routing intent within one
  agent. Tighten a source's **description** first if the orchestrator over-reaches.
- **No pipeline change:** the same chunker/index/upload back both designs; the `url`/`title`
  citation fields are what render the chip.
- **If you later want stronger routing/grounding adherence**, a more capable model (where
  Copilot Studio allows the choice) helps answer quality here - but it does **not** affect
  citation rendering, which is now handled deterministically by the single-agent shape.
