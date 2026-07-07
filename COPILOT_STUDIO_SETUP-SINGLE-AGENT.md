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
- COMPLETENESS ("by product" / "all products" / "across products" / "summary" / "the
  portfolio"): a portfolio question must be answered from the SINGLE "Net Sales by Product"
  summary table for that period and list EVERY printed brand row in one table - not a
  hand-picked two or three. Do NOT answer a by-product question from a few per-brand detail
  slides and then claim "only <X> and <Y> have figures": if you are seeing only 2-3 brands
  (especially ones carrying long comment text), you have likely retrieved per-brand slides,
  NOT the product-summary table. In that case say the list may be PARTIAL and list what you
  have, but NEVER assert or imply it is the complete set unless the product-summary table
  itself is your source. Prefer the widest product table available for the period.
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
- ANSWER ONLY WHAT WAS ASKED (scope discipline): match the response to the dimension the
  user asked for. If they ask for "financial results" / "the figures" / "how did <Brand>
  do" / "net sales", return ONLY the financial figures - do NOT also dump the rest of the
  IR-Notes narrative (NCCN / guideline updates, distribution-channel changes, access,
  catalysts, positioning). Include that broader commentary ONLY when the user asks for the
  "full update" / "external messaging" / "everything" / "IR notes" on the brand.
- OUTPUT SHAPE: open with a SHORT direct answer (1-2 sentences) that answers exactly what
  was asked, THEN the figures/points as a tight bulleted list (one brand or one metric per
  bullet). Never reply with a single long dense paragraph.
- OFFER MORE: after a NARROW answer (e.g. financials only), close with ONE short offer line,
  e.g. "Want the full external-messaging view (positioning, guidance, catalysts, access) for
  Kisqali?" - so the user opts into the broader IR narrative instead of getting it unrequested.
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
Answer like a senior commercial analyst: thorough, quantified, exhaustive on the printed
cuts, and explicit about what is STATED vs INFERRED vs NOT LABELED.
- OPEN WITH A SOURCE LINE: one line naming the report(s), cadence (monthly/weekly), and
  data-through period used (e.g. "Monthly Report, May'26 cut, data through Mar'26 / Apr
  YTD").
- GROUNDING: Quote STATED metrics VERBATIM with units (#, %, K, pts). Never fabricate a
  stated figure, and never invent a discrete-period value the source does not label. If
  the source labels only endpoints (e.g. R4W rolling-4-week and YTD) and not clean
  discrete periods, SAY so rather than inventing month-by-month numbers.
- STATED FIRST, CHART SUPPLEMENTS (never skip the chart): Lead with the STATED figures
  (vs PY, vs TGT, share vs PY / vs TGT, R4W Act / TGT, YTD / MTD) and show ALL stated cuts
  first. You MAY read TREND CHARTS for an APPROXIMATE direction/level - always label as
  approximate + chart-derived + cite the page, kept separate from stated numbers.
  "Prioritize stated over chart" means lead with stated numbers; it does NOT mean skip the
  chart. NEVER end a trend / "vs last quarter" / QoQ / MoM question at "not stated": when
  no stated QoQ delta exists you MUST still (a) give each metric's current LEVEL (stated if
  printed, else the chart level with "~", e.g. NBRx share ~9%) and (b) read the trend chart
  for the approximate QoQ trajectory (e.g. "roughly flat" or "~11% -> 12%, +~1 pt"),
  labeled approximate and citing the chart page(s).
- ALWAYS SHOW LEVELS, NOT JUST DELTAS: give each metric's current level, not only its
  vs-PY delta - e.g. "NBRx share ~9% (-0.9 pts YoY)", never just "-0.9 pts". In the
  Summary Table include the current LEVEL for each metric, not only the vs-PY delta.
- NAME THE MARKET / DENOMINATOR for any share figure (total MS market vs B-Cell /
  anti-CD20 segment vs generalist) - they differ hugely (~15% total MS vs ~63% B-Cell).
  Never quote a bare share % without saying which market it is. DENOMINATOR SANITY CHECK:
  if a chart share level conflicts with the known stated share for that market (e.g. chart
  ~33-37% but stated total-MS share is ~15%), it is a DIFFERENT segment - name that
  segment and do NOT call it "MS market".
- "GAINED OR LOST SHARE" / "HOW DID X DO VS MARKET": NEVER conclude "lost share" from a
  declining ABSOLUTE number or a sub-segment (e.g. generalist) alone. ALWAYS check the
  market benchmark (product growth vs market growth = +/- pts) AND the stated share-vs-PY
  delta first - a brand can OUTPERFORM a falling market while its own metric declines
  (e.g. Kesimpta NBRx -3% vs MS market -14% = +11 pts). Lead with the market-benchmark
  comparison and the stated share-vs-PY delta; use generalist / sub-segment / chart figures
  only as clearly-labeled supplements that never override the stated total-market read.
- SOURCE PRECEDENCE / SHOW BOTH CADENCES: MONTHLY is authoritative for the same period;
  use the weekly Pulse for the freshest read and label it. When both have relevant data,
  present Monthly and Weekly in SEPARATE labeled tables and state what each provides
  (monthly gives YTD aggregates; weekly gives R4W Act vs TGT). State which report + period
  you used.
- BE EXHAUSTIVE ON PRINTED CUTS: for a metric question show ALL related printed cuts -
  vs TGT AND vs PY, share vs TGT AND share vs PY, R4W Act / TGT, and any SEGMENT breakdowns
  printed (1L, LET switch, Med B/D, generalist, eBC / mBC), plus any overall NBRx YoY
  growth %. Use a table: Product | Period | Metric | Value. Put MTD and YTD in SEPARATE
  rows; never combine two values in one cell.
- MARKET & COMPETITOR CONTEXT: when the source prints market growth and/or competitor
  deltas alongside the brand, include them (e.g. "market NBRx -14% vs PY"; "Repatha +1.9,
  Nex/Nex -1.2"). But NEVER rank a COMPETITOR as one of "our" gainers.
- PRIORITY / OUR BRANDS vs COMPETITORS: "priority brands" / "our brands" means the
  Novartis brands (e.g. Kisqali, Kesimpta, Leqvio, Scemblix, Pluvicto, Cosentyx, Lutathera,
  Fabhalta). Competitor brands read off a market-share chart (e.g. Ocrevus, Briumvi,
  Repatha) are CONTEXT ONLY - they are the market, not our portfolio. When the user asks
  for top gainers "across priority brands", rank ONLY Novartis brands and list competitor
  moves separately as market context. Do NOT eyeball a competitor bar and report it as a
  Novartis share gain.
- STATED DELTAS BEAT CHART BARS FOR RANKING: rank on the printed vs-PY / vs-TGT
  share-point deltas (found on the per-brand metric pages) - NOT on approximate levels
  read off a single market-share bar chart. If the only thing available is chart bars, say
  the ranking is approximate/chart-derived and cite that page; but first look for the
  stated per-brand deltas elsewhere in the report.
- RANKING ("top / which / rank gainers"): rank ONLY Novartis priority brands. SCAN ALL
  priority brands, not just the top one - check Kisqali, Kesimpta, Leqvio, Scemblix,
  Pluvicto, Cosentyx, Lutathera, Fabhalta each before concluding, and include EVERY brand
  that has a stated delta (do NOT stop at the single biggest gainer). Give BOTH ranking
  methods (both are MANDATORY - never drop one; if one has no data, still show its header
  and say "no stated data for this method"):
  Method 1: Absolute share change (percentage-point vs PY / vs TGT) - include per-segment
  deltas too (1L, LET switch, Med B/D), each as its own ranked row.
  Method 2: Growth vs market benchmark (product growth vs market growth, with the
  difference in pts) - e.g. Kesimpta NBRx -3% vs MS market -14% = +11 pts.
  Then ALWAYS add a "Not rankable from this source" section (MANDATORY) listing brands that
  show only current share / sales levels WITHOUT a stated vs-PY share-point delta or a
  product/market growth pair (show their current levels so the user sees why).
- DIRECTIONAL SUMMARY: end with a short synthesis - what is gaining vs flat / declining,
  brand-vs-market read, and any "Watchout" (a notable decline or risk printed in the
  source).
- ANSWER SHAPE for simple single-metric lookups: lead with the OVERALL/brand-level metric,
  then offer ONE deeper cut (e.g. "Want the eBC vs mBC split?"); do not open at the deepest
  sub-segment.
- INDICATION / BASIS CLARIFY: if a clarification was resolved by a topic and appended to the
  question (e.g. "...for eBC (early breast cancer)" or "...(r3m basis)"), answer THAT directly
  and state the resolved dimension you used; do not re-ask. If the asked brand is NOT reported
  by indication (no eBC/mBC split exists in the source), IGNORE any eBC/mBC qualifier that was
  appended and answer at the OVERALL brand level - do NOT reply "no data" just because an
  indication was added.
- $ figures (Net Sales in dollars) are NOT here -> use Financial Results.
- If the brand/period is not in the index, say so; never invent metrics.
=== END PRODUCT STRATEGY ===

=== META - domain rules ===
Only boilerplate: disclaimers, cover pages, agendas, reference sections. Quote VERBATIM.
For any substantive financial / messaging / product question, answer from the correct
source above instead. If asked for boilerplate that isn't indexed, say so and stop.
=== END META ===

=== ANSWER ASSEMBLY ===
- DEFAULT SHAPE: open with a 1-2 sentence direct answer to exactly what was asked, THEN
  bullets (or a table where the domain calls for one). Avoid single long dense paragraphs,
  and do not pad a narrow question with unrelated content the user did not ask for.
- Quote numbers verbatim (no rounding, unit, or currency conversion).
- If sources disagree, surface both and label it.
- For multi-part questions, use clear sub-sections.
- Assume USD + US geography and state that assumption; do not ask about it.
- Do NOT append disclaimers, legal notes, or advice caveats (e.g. "for general educational
  purposes only", "consult a licensed financial professional", "before making any investment
  decisions"). This is INTERNAL grounded reporting from indexed company documents, NOT
  investment advice - give the answer and stop.
- Do NOT write any "Sources:" line or inline citation markers - Copilot Studio attaches
  the native citation chip automatically from the indexed url/title fields.
=== END ANSWER ASSEMBLY ===
```

---

## 4. Clarification + follow-up topic (deterministic, ONE topic)

All product-strategy questions already flow through ONE topic on the `FinSight US` agent
(Trigger "agent chooses" → **Create generative answers**). We EXTEND that single topic so it
(a) asks the right clarifier ONLY when needed, and (b) can offer a clickable follow-up -
without creating extra topics. Instruction-based asks can be SKIPPED under generative
orchestration, so a Topic + Question node (which ALWAYS fires) is what guarantees the prompt.

> **Single-agent bonus:** because the SAME agent retrieves and responds, the topic's
> **Create generative answers** node renders the answer WITH its native citation chip. Feed
> that node a composite query and the chip still works - no child hop to drop it.

### The composite-query pattern (the core idea)

Capture the original question BEFORE asking, so a follow-on question never loses it:

```
1. Set  Topic.OriginalQ = System.Activity.Text        (capture BEFORE asking)
2. Ask  the clarifying Question -> Topic.<Choice>      (entity auto-skips if already clear)
3. Set  Topic.Query = Topic.OriginalQ & " " & <resolved clarification>
4. Create generative answers, Input = Topic.Query      (renders answer + native chip)
```

No agent inputs are needed: in the single-agent design the composite query goes STRAIGHT
into the generative-answers node's **Input** field (replacing the default `Activity.Text`).
Without step 1 the node would only see the button text (e.g. "eBC") and lose "NBRx share for
Kisqali" - `Topic.Query` carries the whole intent.

### Entities to create (closed lists)

Two closed-list entities drive the **auto-skip** (Settings → Entities → + Add an entity →
New entity → Closed list). Add items with synonyms via **Add in bulk / upload a file**.

**`PeriodBasis`** (Smart matching OFF is fine - basis terms are fixed):

| Item | Synonyms |
|---|---|
| `months` | months, calendar months, monthly, month by month, last 3 months, past three months |
| `r3m` | R3M, rolling 3 month, rolling three month, rolling, 3-month rolling, trailing 3 months |

**`Indication`** (turn Smart matching **ON** - indication phrasing varies a lot):

| Item | Synonyms |
|---|---|
| `eBC` | early breast cancer, early BC, adjuvant, curative setting, eBC setting |
| `mBC` | metastatic breast cancer, metastatic BC, advanced breast cancer, metastatic setting |
| `both` | both, both indications, overall, total, eBC and mBC, combined, n/a, not applicable, doesn't apply |

The other two "dimensions" need NO entity:
- **"is this a trend ask?"** → a **Condition** on the text (not an entity).
- **follow-up chips** (exclusive vs overlapping, Med B) → just the Question node's buttons.

### The one combined topic - node layout

We add a **skip-condition** before each Ask, so if the user ALREADY stated the indication or
the basis we skip the question and answer directly. (This is belt-and-suspenders on top of
the entity auto-skip - explicit and visible on the canvas.)

```
Trigger (agent chooses - prescription / NBRx / TRx / share)
│
├─ 1. Set  Topic.OriginalQ = System.Activity.Text
│
└─ 2. Condition: TREND ask?  ("trend" | "last 3 periods" | "lately" in OriginalQ)
     │
     ├─ YES (trend) ─ 2a. Condition: basis already stated?
     │     │            ("r3m" | "rolling" | "trailing" | "calendar month" in OriginalQ)
     │     ├─ YES → Set Topic.Query = Topic.OriginalQ                  → Create generative answers
     │     └─ NO  → Ask months / r3m (attach PeriodBasis) → Topic.BasisChoice
     │              Set Topic.Query = Topic.OriginalQ & " (" & Text(Topic.BasisChoice) & " basis)"
     │                                                                → Create generative answers
     │
     └─ NO (metric) ─ 3a. Condition: answer directly? (skip the eBC/mBC ask)
           │            YES for "share" / "market share" questions, or when an indication /
           │            segment is already named (eBC, mBC, Med B/D, exclusive, overlapping,
           │            generalist, segment). No brand names - keyed on the QUESTION TYPE.
           ├─ YES → Set Topic.Query = Topic.OriginalQ                  → Create generative answers
           └─ NO  → Ask eBC / mBC / Both / overall (attach Indication) → Topic.IndicationChoice
                    Set Topic.Query = Topic.OriginalQ & " for " & Switch(Text(Topic.IndicationChoice),
                        "eBC","eBC (early breast cancer)",
                        "mBC","mBC (metastatic breast cancer)",
                        "Both / overall","both eBC and mBC indications (overall)","")
                                                                       → Create generative answers
```

Each leaf ends in its OWN **Create generative answers** (Input = `Topic.Query`) because
Copilot Studio condition branches do NOT auto-merge - that node is duplicated into all four
leaves.

> **Leaner alternative (2 answer nodes instead of 4):** drop the inner "already stated?"
> conditions and let the **entity auto-skip** (attached to each Ask) skip the question when
> the value is already present. To avoid re-appending a value the user already typed, make the
> Set-Query formula conditional, e.g.:
> ```powerfx
> If(("ebc" in Topic.OriginalQ) Or ("mbc" in Topic.OriginalQ),
>    Topic.OriginalQ,
>    Topic.OriginalQ & " for " & Switch(Text(Topic.IndicationChoice),
>        "eBC","eBC (early breast cancer)","mBC","mBC (metastatic breast cancer)",
>        "Both / overall","both eBC and mBC indications (overall)",""))
> ```
> Same "skip if already given" behaviour, far fewer nodes. The explicit conditions above are
> just the more visible, deterministic version.

### Build it step by step (manual)

**A. Create the `Indication` entity** (you already have `PeriodBasis`)
1. Left nav → **Settings → Entities** (same place `PeriodBasis` lives).
2. **+ Add an entity → New entity → Closed list**; name it `Indication`; set **Smart
   matching = ON**.
3. Add the three items + synonyms from the table above (use **upload a file / Add in bulk**).
   Save.

**B. Open the topic**
4. **Topics** → open your **Product Strategy** topic (Trigger → Create generative answers).

**C. Capture the original question**
5. Click the **+** just under the **Trigger** → **Variable management → Set a variable value**.
6. Set variable = new `Topic.OriginalQ` (String); **To value → Formula** → `System.Activity.Text`.

**D. Ask the indication clarifier**
7. **+** below that → **Ask a question**.
8. Message: `Quick check - is that for eBC (early breast cancer), mBC (metastatic), or both?`
9. Identify → **Multiple choice options**; add exactly: `eBC`, `mBC`, `Both / overall`.
10. Also under Identify, attach the **`Indication`** entity (this is what auto-skips the ask
    when the user already stated an indication).
11. Save the user response as `Topic.IndicationChoice`.

**E. Build the composite query**
12. **+ → Set a variable value**; new `Topic.Query` (String); **To value → Formula**:
```powerfx
Topic.OriginalQ & " for " & Switch(Text(Topic.IndicationChoice),
    "eBC",            "eBC (early breast cancer)",
    "mBC",            "mBC (metastatic breast cancer)",
    "Both / overall", "both eBC and mBC indications (overall)",
    "")
```

**F. Point the answer node at the composite query**
13. Select the EXISTING **Create generative answers** node.
14. Change **Input** from `Activity.Text` to **`Topic.Query`** (pick the variable / type
    `=Topic.Query`). Leave **Data sources** as-is. Save.

**G. Test**
15. Test pane: "latest NBRx share for Kisqali" → asks eBC / mBC / Both / overall, then
    answers with a citation chip. "...Kisqali eBC" → skips the ask.

**Optional - branch by ask type + SKIP when already stated**
The straight path in A-G asks indication every time. To (i) ask the RIGHT clarifier and
(ii) SKIP it when the user already stated the value, wrap the asks in conditions:
1. After step 6 (Set `Topic.OriginalQ`), add **Condition: TREND ask?**
   `("trend" in Topic.OriginalQ) Or ("last 3 periods" in Topic.OriginalQ) Or ("lately" in Topic.OriginalQ)`
2. In the **NO / metric** branch, add **Condition: answer directly? (skip the eBC/mBC ask)**
```
("share" in Topic.OriginalQ)
 Or ("ebc" in Topic.OriginalQ) Or ("mbc" in Topic.OriginalQ)
 Or ("med b" in Topic.OriginalQ) Or ("med d" in Topic.OriginalQ) Or ("part b" in Topic.OriginalQ)
 Or ("exclusive" in Topic.OriginalQ) Or ("overlapping" in Topic.OriginalQ)
 Or ("generalist" in Topic.OriginalQ) Or ("segment" in Topic.OriginalQ)
```
   Keyed on the QUESTION TYPE, not the brand (brands change over time). Any "share" /
   "market share" question answers directly - it leads with NBRx + TRx share (per the ANSWER
   SHAPE rule) and the agent OFFERS the eBC/mBC split softly in its answer, so no hard ask is
   needed. We also skip when an indication / segment is already named. The eBC/mBC hard ask
   now only fires for a bare count question (e.g. "Kisqali NBRx") with no share / segment /
   indication word. `in` is case-insensitive; `"share"` also matches "market share" and
   "NBRx share".
   - **YES** → Set `Topic.Query = Topic.OriginalQ` → Create generative answers (Input `Topic.Query`).
   - **NO**  → the indication Ask (steps 8-11) → Set Query (step 12) → Create generative answers.
3. In the **YES / trend** branch, add **Condition: basis already stated?**
   `("r3m" in Topic.OriginalQ) Or ("rolling" in Topic.OriginalQ) Or ("trailing" in Topic.OriginalQ) Or ("calendar month" in Topic.OriginalQ)`
   - **YES** → Set `Topic.Query = Topic.OriginalQ` → Create generative answers.
   - **NO**  → Ask months / r3m (attach `PeriodBasis`) → `Topic.BasisChoice`; Set
     `Topic.Query = Topic.OriginalQ & " (" & Text(Topic.BasisChoice) & " basis)"` → Create generative answers.
- Branches don't merge, so each of the four leaves ends in its own Create generative answers
  node (Input `Topic.Query`). Want fewer nodes? Use the entity-auto-skip + `If()` formula
  from the Leaner alternative above and keep only the top-level trend/metric split.

**Optional - add the follow-up chip**
- After the answer, **Ask a question**: "Want the exclusive vs overlapping population
  split?" → options `Exclusive vs overlapping` / `No thanks` → `Topic.FollowupChoice`.
- **Condition** `Topic.FollowupChoice = "Exclusive vs overlapping"` → Set
  `Topic.Query2 = Topic.Query & " - exclusive vs overlapping population"` → another
  **Create generative answers** (Input `Topic.Query2`).

### Verify after Publish (new chat)

| Test input | Expected |
| --- | --- |
| "latest NBRx share for Kisqali" | Asks eBC / mBC / Both / overall → answers overall eBC (with citation chip) → (optional) offers exclusive vs overlapping |
| "latest NBRx share for Kisqali eBC" | Skips the ask (entity auto-fills) → answers overall eBC |
| "latest NBRx share for Leqvio" (no indication split) | Asks → user picks Both / overall → answers overall (guard prevents "no data") |
| "TRx trend for Kesimpta over last 3 periods" | (trend branch) Asks months / R3M → answer STATES the basis |
| "how has Leqvio market share changed vs last quarter?" | "share" matches → answers directly with NBRx AND TRx share (no ask) |

### Financials: clarify the period (Month / YTD / Full year) - separate topic

Financials `$` questions ("net sales", "GTN / gross-to-net", "gross margin", "OPEX",
"operating income", "cost") are ambiguous on PERIOD - a single month vs quarter/YTD vs
full-year outlook are DIFFERENT numbers. The FINANCIAL RESULTS domain rules already say to
ask Monthly / Quarterly (YTD) / Full year (outlook) when none is stated - this makes that
deterministic. EXTEND your EXISTING Financials topic (Trigger -> Create generative answers) -
exactly like you did for Product Strategy; no new topic needed. You insert the clarify nodes
between the Trigger and the existing answer node.

**Entity `PeriodScope`** (Closed list; Smart matching ON):

| Item | Synonyms |
|---|---|
| `Month` | month, monthly, single month, MTD, month to date, for the month, January, February, March, April, May, June, July, August, September, October, November, December |
| `YTD` | YTD, year to date, quarter, quarterly, QTD, Q1, Q2, Q3, Q4, so far this year |
| `Full year` | full year, full-year, FY, outlook, latest outlook, LO, annual, for the year |

**Trigger** - your EXISTING Financials topic trigger already catches these `$` questions (no
change needed). For reference it should describe:
```
The user asks for a US $ financial figure (net sales, GTN / gross-to-net, gross margin,
OPEX, operating income, cost). NOT prescription metrics (NBRx / TRx / share) or messaging.
```

Node layout (one clarifier, so simpler than the Product Strategy topic - 2 leaves):
```
Trigger (agent chooses - $ figures: net sales / GTN / margin / OPEX / operating income)
│
├─ 1. Set Topic.OriginalQ = System.Activity.Text
│
└─ 2. Condition: period already stated?  (skip the ask)
     ├─ YES → Set Topic.Query = Topic.OriginalQ                    → Create generative answers
     └─ NO  → Ask Month / YTD / Full year (attach PeriodScope) → Topic.PeriodChoice
              Set Topic.Query = Topic.OriginalQ & " " & Switch(Text(Topic.PeriodChoice),
                  "Month",     "single month MTD period_scope month",
                  "YTD",       "year-to-date YTD period_scope ytd",
                  "Full year", "full year outlook Latest Outlook period_scope full_year", "")
                                                                  → Create generative answers
```

> **Month vs YTD are near-duplicate slides** (the single-month "March Net Sales" table and the
> "March YTD Net Sales" table score almost identically in retrieval - e.g. rerank 2.728 vs
> 2.723). Two rules make the clarifier separate them:
> 1. **Use POSITIVE index-vocabulary tokens.** The chunks literally carry `period_scope=month`
>    / `period_scope=ytd` / `period_scope=full_year` - appending `period_scope month` gives
>    BM25 an exact keyword hit on the month rows. That's why the Switch appends those tokens.
> 2. **NEVER negate** ("not YTD", "not Q1"). Azure AI Search has no NOT - the words `YTD` /
>    `Q1` in the query just MATCH the YTD chunks and pull them UP the ranking (verified: adding
>    "not YTD not Q1" surfaced the YTD pages instead of suppressing them). Keep month queries
>    free of any YTD / Q1 / quarter words.

**Period skip-condition** (answer directly when a period is already named). A long chain of
`Or` throws **"Max call depth exceeded"** in Power Fx, so use ONE `IsMatch` with a regex
alternation instead (single call, no nesting). `Lower(...)` handles case; `MatchOptions.Contains`
matches anywhere in the text. Use full month NAMES, NOT 3-letter abbreviations - `mar` / `dec`
/ `may` collide with *mar*gin / *dec*line / *may*be:
```powerfx
IsMatch(
    Lower(Topic.OriginalQ),
    "q1|q2|q3|q4|quarter|ytd|year to date|qtd|mtd|month|monthly|full year|outlook|half|h1|h2|january|february|march|april|june|july|august|september|october|november|december",
    MatchOptions.Contains
)
```
("may" is omitted from the regex and left to the `PeriodScope` entity's auto-skip so it
doesn't match "maybe". `month` / `monthly` catch "last month", "this month", "monthly" so a
month ask skips the clarifier and answers the latest month directly - the `SINGLE-MONTH`
chunk banner makes "month" retrieve the month table on its own. Tip: convert ANY condition
that hits "Max call depth" - e.g. the Product Strategy skip-conditions - to this same
`IsMatch(Lower(...), "a|b|c", MatchOptions.Contains)` form.)

**Build it step by step (manual)** - same shape as the Product Strategy topic:
- **A. Entity:** Settings -> Entities -> + Add an entity -> New entity -> Closed list ->
  `PeriodScope`; Smart matching ON; add the three items + synonyms above.
- **B. Open your EXISTING Financials topic** (Topics -> your Financials topic; it already has
  Trigger -> Create generative answers). You insert nodes between them and REUSE the existing
  answer node for one branch.
1. **+ under Trigger -> Set a variable value:** `Topic.OriginalQ` -> Formula `System.Activity.Text`.
2. **+ -> Add a condition:** paste the period skip-condition formula above.
   - **True (period stated):** Set a variable value `Topic.Query` (String) -> Formula
     `Topic.OriginalQ`; then keep your **existing Create generative answers** in this branch
     and change its **Input** to `Topic.Query`.
   - **All other conditions:** **Ask a question** ->
     `Which period - a single month, quarter (YTD), or full-year outlook?` -> Multiple choice
     options `Month`, `YTD`, `Full year`; attach the **`PeriodScope`** entity; save as
     `Topic.PeriodChoice`. Then **Set a variable value** `Topic.Query` -> the Switch formula
     above. Then add a **second Create generative answers** (Input `Topic.Query`).
3. Save.

**Verify:**

| Test input | Expected |
|---|---|
| "GTN for Pluvicto" | Asks Month / YTD / Full year -> answers for the chosen scope |
| "GTN for Pluvicto YTD" | Skips the ask (period stated) -> answers YTD |
| "Kisqali net sales in March" | Skips the ask (month named) -> answers the month |
| "Operating income full year outlook" | Skips -> answers full-year outlook |

**Other Financials clarifiers you can add the SAME way (optional):**
- **Measure basis** - Actual / Latest Outlook (LO) / Target. Only worth a hard ask if users
  mix forecasts with actuals; the domain rule already defaults to *actual*. Entity
  `MeasureBasis`; skip when "actual", "outlook", "lo", "target", "budget", "tgt" already in text.
- **Comparator** - vs Prior Year / vs Target / vs Latest Outlook, for "variance / how did X
  do" questions with no comparator named. Entity `Comparator`; skip when "vs py", "vs tgt",
  "vs target", "vs lo", "prior year" already present.
Each slots in as its own condition -> ask -> Set-Query -> answer leaf, exactly like the
period one (every new leaf duplicates the answer node, since branches don't merge).

### External Messages: answer narrow first, then offer an IR follow-up chip

Feedback on IR-notes questions ("latest financial results we shared for Kisqali"): the reply
was one long paragraph AND it bundled the whole IR-Notes section (NCCN, distribution-channel
changes, access) when only the figures were asked. The **instruction** changes above fix the
default (short paragraph -> bullets; answer ONLY what was asked; then a one-line offer). This
topic makes the follow-up deterministic - answer the narrow question, THEN show a clickable
chip so the user opts into the deeper IR cut instead of receiving it unrequested.

> Do NOT hard-ask BEFORE the first answer here. External-messaging questions usually have a
> clear direct answer (the figures) - asking up front would be the exact "super long / too
> much" problem in reverse. Answer first, offer second.

**Entity `QuantData`** (Closed list; Smart matching ON) - the IR quantitative cuts from the
feedback:

| Item | Synonyms |
|---|---|
| `Sales guidance` | sales guidance, guidance, revenue guidance |
| `Growth rate` | growth rate, growth, % growth, YoY growth, vs PY growth |
| `Peak sales estimate` | peak sales, peak sales estimate, peak revenue |
| `Market size` | market size, TAM, addressable market, patient pool |
| `Target / outlook metric` | target, outlook, latest outlook, LO, guidance range, forecast |

Node layout (EXTEND your EXISTING External Messages topic - Trigger -> Create generative
answers; you ADD the follow-up chip AFTER the existing answer node, you do not gate the first
answer):
```
Trigger (agent chooses - external messaging / IR notes / guidance)
│
├─ 1. Set Topic.OriginalQ = System.Activity.Text
├─ 2. Create generative answers   (Input Activity.Text)  <- your EXISTING answer node, unchanged
│         (instruction now returns: short paragraph + bullets, figures only, + offer line)
│
└─ 3. Ask a question: "Which quantitative data should I extract?"
        options: Sales guidance | Growth rate | Peak sales estimate | Market size |
                 Target / outlook metric | No thanks         (attach QuantData) -> Topic.QuantChoice
     │
     └─ Condition: Topic.QuantChoice <> "No thanks"
          └─ YES → Set Topic.Query2 = Topic.OriginalQ & " - " & Text(Topic.QuantChoice)
                   → Create generative answers   (Input Topic.Query2)
```

**Build it (manual):**
- **A. Entity:** Settings -> Entities -> + New entity -> Closed list -> `QuantData`; Smart
  matching ON; add the five items + synonyms above.
- **B. Open your EXISTING External Messages topic.** Keep Trigger -> **Create generative
  answers** as-is (Input `Activity.Text`). BEFORE that answer node add **Set a variable
  value** `Topic.OriginalQ` -> Formula `System.Activity.Text` (so the follow-up can reuse the
  original question).
- **C. AFTER the answer node**, add **Ask a question**: `Which quantitative data should I
  extract?` -> Multiple choice `Sales guidance`, `Growth rate`, `Peak sales estimate`,
  `Market size`, `Target / outlook metric`, `No thanks`; attach `QuantData`; save as
  `Topic.QuantChoice`.
- **D.** Add a **Condition** `Topic.QuantChoice <> "No thanks"` -> **Set a variable value**
  `Topic.Query2` (String) -> Formula
  `Topic.OriginalQ & " - " & Text(Topic.QuantChoice)` -> a **second Create generative
  answers** (Input `Topic.Query2`). Save.

**Verify:**

| Test input | Expected |
|---|---|
| "latest financial results we shared for Kisqali" | SHORT paragraph + bulleted figures only (no NCCN / distribution dump), then the offer line, then the "Which quantitative data?" chip |
| pick "Peak sales estimate" | Answers the peak-sales cut for Kisqali from the IR notes (with citation chip) |
| pick "No thanks" | Ends cleanly, no extra answer |
| "full external messaging update on Kisqali" | Broad IR narrative (NCCN, distribution, access) IS included - user asked for the full update |

---

## 5. Test & publish

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

## 6. Notes

- **Routing precision:** the guard rule "$ figures = Financial Results only; ignore $-like
  numbers from other sources" reproduces the multi-agent hard-routing intent within one
  agent. Tighten a source's **description** first if the orchestrator over-reaches.
- **No pipeline change:** the same chunker/index/upload back both designs; the `url`/`title`
  citation fields are what render the chip.
- **If you later want stronger routing/grounding adherence**, a more capable model (where
  Copilot Studio allows the choice) helps answer quality here - but it does **not** affect
  citation rendering, which is now handled deterministically by the single-agent shape.
