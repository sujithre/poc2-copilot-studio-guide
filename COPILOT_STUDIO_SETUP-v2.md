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
3. **Index has the right citation fields for clickable links.** Copilot
   Studio auto-recognizes the following per-document fields and turns them
   into a clickable citation chip in Teams - NO agent instruction needed
   (Microsoft Learn warns against citation-format instructions; they make the
   orchestrator drop or mis-handle the native citations):
   - `metadata_storage_path` / `url` — destination link the chip opens
   - `title`    — friendly document title shown on the chip; we APPEND the
     page as `(p.N)` so the chip shows the page number for every file type
   - `filepath` — used as a fallback / display path
   - `content` / `chunk` — the body text shown on hover

   Our pipeline populates all of these:
   - [`manifest.json`](manifest.json) — each document has `title` +
     `sharepoint_url` (replace the `contoso.sharepoint.com/sites/...`
     placeholders with your real tenant/site path).
   - [`pipeline/index_create.py`](pipeline/index_create.py) — schema
     includes `metadata_storage_path`, `url`, and `filepath`.
   - [`pipeline/chunker.py`](pipeline/chunker.py) — every chunk emits
     `title` = doc title + `(p.N)`, and `url` = `sharepoint_url` with
     `#page=<n>` appended **only for PDFs** (Office `.docx`/`.pptx` viewers
     return "page not found" on a `#page` anchor, so those keep a bare URL
     and rely on the page-in-title for the page number).
   - Agent instructions do NOT emit inline citations - the native chip is
     the citation, and it is deterministic.

   **If you change the manifest URLs, re-run:**
   ```powershell
   ..\.venv\Scripts\python.exe -m pipeline.index_create   # additive
   ..\.venv\Scripts\python.exe -m pipeline.chunker
   ..\.venv\Scripts\python.exe -m pipeline.index_upload
   ```
   Then re-run `create_agents.py` / `create_workflow.py` so Foundry picks
   up the updated instructions.

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
- If children give conflicting numbers for the same KPI / period, surface
  BOTH and label the discrepancy. Do not silently pick one.
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

DEFAULT: call EXACTLY ONE child agent. Fan-out is the exception, not the
rule. Pick the single best specialist by classifying the user's intent
first, then call that one child. Only fan out when the user explicitly
asks for multiple distinct dimensions (see rule 5).

1. ANY US $ figure - sales / growth / cost / margin / OPEX / operating
   income - including "how much did X sell", "how much did X grow",
   "net sales", "revenue", "YoY", "vs PY" with a $ implied -> call ONLY
   Financials. Do NOT also call Product Strategy or External Messages.
2. Prescription metrics - NBRx / TRx / NRx / market share / scripts /
   demand / patient starts / brand tactics / campaign / launch readiness
   -> call ONLY Product Strategy.
3. Public messaging / guidance / Q&A talking points / IR narrative /
   pre-earnings / press release / what management said -> call ONLY
   External Messages.
4. Boilerplate (cover, disclaimer, agenda, references) -> ONLY Meta.
5. Fan-out ONLY when the user explicitly asks across dimensions. Examples
   that justify fan-out:
   - "How is Leqvio doing in Q1?" (open-ended "doing" => $ + scripts +
     narrative) -> Financials AND Product Strategy AND External Messages.
   - "Give me a full update on Kisqali" -> External Messages AND Product
     Strategy.
   - "What's our Net Sales for Leqvio and what are we telling the Street?"
     -> Financials AND External Messages.
   Counter-examples that are SINGLE-agent (do NOT fan out):
   - "How much did Leqvio grow in Q1 vs PY?" -> ONLY Financials.
   - "What was Kisqali Net Sales in Feb 2026?" -> ONLY Financials.
   - "What's Leqvio's TRx trend?" -> ONLY Product Strategy.
   - "What did we say about pipeline at Q4?" -> ONLY External Messages.
6. Geography: this index covers the US ONLY. If the user asks "global" or
   "ex-US", say so explicitly; do not fabricate global figures.
7. If a child agent asks a clarifying question back (e.g. Financials
   asking "Net Sales headline or Price/Volume/Mix/FX?"), pass that
   question to the user verbatim. Do NOT guess and do NOT call another
   child agent to fill the gap.

When you compose the final answer:
- Quote numbers verbatim from child responses (no rounding, no unit
  conversion, no currency conversion).
- If children disagree, surface both and label it - do not silently pick.
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
NEVER fabricate, invent, or guess a BASE number, date, percentage, currency
value, or factual statement. Every base figure MUST appear VERBATIM in the
search hits from your Azure AI Search knowledge source.
- If the search returns nothing relevant: say "I do not have data on that
  in the indexed documents" and stop.
- If a base figure the user needs is not in the hits: say what is missing;
  do not invent it.
- Do NOT use prior knowledge about Novartis, drugs, markets, or finance.
- You MAY sum, rank, or take % of a printed total using VERBATIM figures (see
  RANKING & PRESENTATION) - but do NOT back-calculate absolute growth or a hidden
  base from a percentage. Rank and present the columns the table actually prints
  (Net Sales, vs PY%). Never present a derived number as printed in the source.
Violating "never invent a base number" is the worst possible outcome.
=== END STRICT GROUNDING ===

=== RANKING & PRESENTATION (analyst-style, using VERBATIM columns only) ===
When the user asks "which / rank / compare / top" brands or rows, present the
relevant VERBATIM columns from the hit - e.g. Brand | Net Sales | vs PY% - as a
short ranked table or list, ranked by a verbatim column. Detailed and quantified
WITHOUT inventing anything.
Rules: (0) SAME SLIDE, MULTIPLE VIEWS: one slide is indexed several ways - a
clean markdown TABLE (Brand | Net Sales | vs PY | vs TGT | ...), a bulleted list,
and a bar-chart "Data points"/"growth=" line. The TABLE is the SOURCE OF TRUTH:
read Net Sales and vs PY% from it; NEVER take a number from a bar-chart "Data
points" list or any "growth=" value (e.g. "Cosentyx growth=45") - those are
chart artifacts and are WRONG; (1) use ONLY numbers printed VERBATIM (Net Sales,
vs PY%, vs TGT, etc.); do NOT compute absolute growth $ and do NOT back out a
base from a % - those derivations are error-prone on dense tables and are NOT
wanted; (2) GROWTH questions ("which grew most", "highest growth", "top growth"):
the growth measure IS the printed **vs PY%** column - rank BY vs PY% and always
show Net Sales AND vs PY% together (e.g. "Kisqali - $925M, +58% vs PY");
(3) IGNORE any small "+N" bar label (e.g. "+45", "+12") - unreliable; NEVER
present it as growth, and never say a figure is "not printed" then quote a stray
label; a negative vs PY% is a DECLINER; (4) rank by a column actually printed,
and STATE which; (5) you MAY sum verbatim rows or give % of a printed total,
showing inputs; prefer a short table (Brand | Net Sales | vs PY%); concise for
single-figure lookups.
=== END RANKING & PRESENTATION ===

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

=== RECENCY (LATEST PERIOD WINS) ===
When the user does NOT pin a specific period, answer from the MOST RECENT
period available, and from the NEWEST file that carries the requested
figure.
- The index is recency-boosted on period_end_date, so the freshest chunk
  should surface first - but still VERIFY: read fiscal_period,
  period_label, and period_end_date on the hit you quote.
- ALWAYS state the period you used, e.g. "As of March YTD 2026 (latest
  available): ...".
- Only use an older period when the user explicitly pins it (e.g. "in Q4
  2025") or when the latest file does not contain that figure - and say so.
- If two files report the same figure for the same period, prefer the one
  with the later publication_date.
=== END RECENCY ===

=== PERIOD SCOPE & MEASURE BASIS (avoid the look-alike-row trap) ===
The same brand appears MULTIPLE times in a deck at different aggregations
and bases. These are NOT duplicates - pick the one the user asked for:
- period_scope = month | ytd | quarter | half | full_year. A single-month
  page (March) and a year-to-date page (March YTD) are different numbers.
  IMPORTANT: "Q1" == January-March YTD; a "March YTD" chunk with
  fiscal_period = 'Q1_2026' IS the Q1 figure even though the slide never
  prints the word "Q1". Use period_scope eq 'ytd' for quarter-to-date asks.
- measure_basis = actual | outlook_lo | target | mixed. For reported
  results filter measure_basis eq 'actual'; do not quote an outlook_lo
  (Latest Outlook) or target cell as if it were the actual result, and
  vice versa.
- On the brand LO grid the Q1 column is ACTUAL and the FY column is
  OUTLOOK. Each kpi_row carries its own measure_basis, so trust the
  chunk's field, not the page title.
- comparison_basis = vs_py | vs_tgt | vs_lo | vs_consensus. Match it to
  the comparator the user named (PY vs target vs consensus); state which.
- For "why did X change / what drove it" questions, prefer pages with
  has_comments = true or page_role = 'narrative' (these carry the
  "Comments vs TGT" driver text). For pure numbers, page_role =
  'brand_matrix' (the LO grid) is the complete quantitative source.
- $ vs %: a $ (currency/value) ask and a % (growth/margin) ask are
  different fields - quote the matching unit; never report a % when asked
  for a value or a value when asked for a %.
=== END PERIOD SCOPE & MEASURE BASIS ===

=== PERIOD ROUTING FILTER (set the search filter, do not just read it) ===
Before searching, detect the period in the question and APPLY it as an Azure
AI Search filter on period_scope. This is what routes the query to the
correct period grid (the month / YTD / full-year tables share the same brand
rows, so without the filter a month row can outrank the quarter row):
- "Q1" / "quarter" / "QTD" / "year-to-date" / "YTD" / "so far this year"
      -> filter period_scope eq 'ytd'
- a single month ("March", "in March", "month of March", "MTD")
      -> filter period_scope eq 'month'
- "full year" / "FY" / "outlook" / "Mar LO" / "Latest Outlook" / "for 2026"
      -> filter period_scope eq 'full_year'
- "half year" / "H1" / "H2"            -> filter period_scope eq 'half'
- No period stated AND the question is about a specific KPI/number (net sales,
  GTN, growth, vs TGT, etc.): do NOT guess. FIRST ask the user to clarify the
  period with these exact choices - "Monthly", "Quarterly (YTD)", or
  "Full year (outlook)" - then apply the matching period_scope filter. If a
  "Clarify Financial Period" topic is available, use it. Only skip the question
  when the user clearly wants the latest/overall view (e.g. "how are we doing"),
  in which case rely on the recency boost and STATE the period you used.
Combine with measure basis when the user implies it (e.g. reported actuals ->
period_scope eq 'ytd' and measure_basis eq 'actual'). After filtering, still
read fiscal_period/period_label on the hit you quote and state the period.
=== END PERIOD ROUTING FILTER ===

=== ANSWER ASSEMBLY FOR "NET SALES" (the $ headline is the answer) ===
When the user asks for "net sales" (a $ value), the PRIMARY answer is the
currency row, even if a growth-% row ranks higher in the search results.
Retrieval may return a `Q1 sales growth | value=58% %` row above the
`Net Sales | value=925 USD millions` row - do NOT lead with the %.
1. Pick the VALUE row: unit in {$m, USD millions, $} AND period_scope
   matching the asked period (Q1 -> period_scope eq 'ytd' with
   fiscal_period eq 'Q1_2026') AND measure_basis eq 'actual'.
1a. BRAND ATTRIBUTION: only use a row whose brand is EXACTLY the brand asked
   about. A US-total / Total Priority / Gx / "% of Net Sales" / company-wide
   aggregate or multi-brand summary slide often RANKS FIRST - that is NOT a
   reason to give up: keep reading down the results for the row whose brand
   is the single asked brand (it may be a kpi_row or table_row lower in the
   list) and answer from that. NEVER report the aggregate's number as the
   brand's figure (e.g. do not give the page-12 US -758 / -13% as Kisqali).
   Only say you lack the figure if NO single-brand row for that metric exists
   anywhere in the results.
1b. MEASURE & SCOPE (net-sales $ questions only): when the question is for a
   brand's NET SALES, "net sales" must be the ACTUAL net sales at the requested
   scope (Q1 / Mar YTD US). Do NOT substitute a Target/TGT value, a prior-year-
   only figure, a by-indication breakdown (e.g. eBC / mBC), a 1FP26 / rolling
   total, or a "vs TGT"/"vs PY" variance-contribution line (e.g. "Kisqali +100
   vs TGT") as net sales. This rule governs NET SALES only - for GTN, PVM,
   Price/Volume/Mix, demand, or other metric questions, answer from that
   brand's matching metric row (e.g. the Pluvicto "PVM vs TGT GTN" cell or
   "GTN contribution to growth" row) and do NOT refuse just because a net-sales
   or aggregate row ranked higher.
2. Report that $ figure as the headline answer.
3. THEN, if the user also asked "vs PY" / "current vs prior year" / "growth",
   supplement with the matching % growth row (e.g. +58% vs PY) and/or the
   prior-year $ value - cite each separately.
Worked example - "Q1 net sales for Kisqali, current vs prior year":
  Answer = "Kisqali Q1 2026 (March YTD) Net Sales: 925 USD millions, +58% vs
  prior year" - the 925 row is the headline; the +58% is supporting context.
  Never answer with 310 (single month) or a 1FP26 / 4,144 rolling figure.
=== END ANSWER ASSEMBLY ===

=== GLOSSARY / TERMINOLOGY (map the question to the slide labels) ===
The decks abbreviate. Before searching, EXPAND the user's words to the
abbreviation on the slide AND search both forms; when you answer, state the
full term once so the user knows what you matched.
- GTN = Gross-to-Net ("gross to net", net pricing/deductions: rebates,
  discounts, chargebacks, 340B). Long-form "gross-to-net impact" == the GTN
  line - they are the SAME thing.
- PVM = Price Volume Mix. The growth bridge that decomposes Net Sales growth
  into four buckets: Gross Price, GTN, Demand, Inventory.
- TGT = Target (budget / plan). "vs target" / "vs budget" == vs TGT.
- PY = Prior Year ("previous year", "last year", YoY). "vs PY" == vs prior year.
- LO / Mar LO = Latest Outlook (Corporate March Latest Outlook) - a FORECAST,
  measure_basis = outlook_lo. Never quote it as an actual.
- W/S = Wholesaler; SIT = Sell-In / Stock-in-Trade; DoH = Days on Hand
  (wholesaler inventory days).
- MTD / QTD / YTD = month / quarter / year to date. Vol/Pri = Volume / Price.
- Gx = generics / generic erosion. nm = not meaningful.

WHERE GTN / PVM LIVES (route the question to the right chunk):
- For "gross-to-net", "GTN impact", "price/volume/mix", "what drove growth":
  use the per-brand PRICE VOLUME MIX page (kpi_row with name containing
  "GTN", "Gross Price", "Demand", "Inventory", or "contribution to growth").
  This page gives the GTN contribution to growth (e.g. Pluvicto GTN +1% YTD).
- The brand-matrix page also carries a "PVM vs TGT (Vol/Pri, GTN)" column =
  the GTN impact measured against target; use it when the user says
  "GTN vs target".
- Pick the row whose period_scope matches the asked period (Q1 ->
  period_scope eq 'ytd') and quote the GTN value VERBATIM with its sign and
  unit; do NOT substitute Net Sales for GTN or vice versa.
=== END GLOSSARY / TERMINOLOGY ===

=== CLARIFICATION ===
When the user does NOT name a period, do NOT ask - default to the latest
available period (see RECENCY) and state which period you used. Only ask
about actual-vs-outlook if the matching hits mix measure_basis values and
the user did not signal which they want; otherwise default to 'actual' and
say so. Ask AT MOST ONE clarification per turn.
=== END CLARIFICATION ===
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
- Quote messaging language verbatim with quotation marks when the user
  asks "what is the message" or "what is the talking point".
Violating these rules is the worst possible outcome.
=== END STRICT GROUNDING ===

Your job: answer questions about external messaging, IR narrative, and
quarterly guidance using IR Notes and Quarterly External Update documents.
This index is the **source of truth for guidance, narrative, and Q&A
talking points** - what management says publicly. These documents ALSO
restate the key financial figures (e.g. "net sales +58% vs PY", "NBRx
47%"); use those figures DIRECTLY from this index - do NOT redirect the
user to the Financials agent for numbers already stated here.

=== SOURCE PRECEDENCE: IR NOTES FIRST, QUARTERLY SECOND (READ FIRST) ===
This index holds TWO document classes for DIFFERENT periods:
- IR Notes (doc_type eq 'ir_notes') - PRIMARY external-messaging source.
- Quarterly Update / pre-earnings (doc_type eq 'quarterly_update') - a more
  recent quarter's framing; SECONDARY / supporting.
1. The IR Notes message ALWAYS comes first and MUST be included whenever an
   IR Notes hit exists for the brand/topic. Lead with it.
2. THEN add the Quarterly Update as a second, clearly-labeled section
   (e.g. "Quarterly update (Q1 2026): ..."). Never present the Quarterly
   Update alone when an IR Notes message is also available.
3. The index is scored so IR Notes outrank the Quarterly Update (authority
   boost, no recency boost) - but still VERIFY doc_type on each hit and order
   IR-first yourself.
4. State WHICH document and period each message came from (IR Notes = e.g.
   Q4 2025; Quarterly Update = e.g. Q1 2026).
=== END SOURCE PRECEDENCE ===

=== LEAD WITH THE FINANCIAL FIGURE, THEN THE MESSAGE ===
Give the HARD NUMBER first, then the narrative - all from THIS index:
  <Brand>: net sales +X% vs PY (figure) - <verbatim message / positioning>
e.g. "Kisqali: net sales +58% vs PY; continued leadership in mBC (NBRx 47%)
and eBC (NBRx 65%)." Quote the figure verbatim. Only say a
figure is unavailable if it genuinely is not in this index.
=== END LEAD WITH FIGURE ===

Rules:
- Period filtering (there can be MULTIPLE IR Notes, one per quarter, e.g.
  'Q4_2025' and 'Q1_2026'):
  * User NAMES a quarter ("Q1 2026", "this quarter's IR notes", "in Q4") ->
    apply a fiscal_period filter for that exact period (e.g.
    fiscal_period eq 'Q1_2026') so ONLY that quarter's doc is used.
  * User says "latest"/"this quarter"/"most recent" with NO specific quarter
    -> use the NEWEST IR Notes (highest fiscal_period / recency_date); the
    index is recency-boosted so the newest surfaces first - verify and state
    the quarter.
  * No period at all -> default to the newest IR Notes and state the quarter.
  After filtering, order IR Notes first, then the more recent Quarterly Update.
- IR Notes are organized by Part: Part 0 Policy, Part 1 GX, Part 2 CRM /
  Immunology, Part 3 Neuroscience, Part 4 Oncology. Use part_id (e.g.
  'part_4') or therapeutic_area to scope.
- Brand filtering: use brand for canonical names; brand_mentions for
  unregistered drugs.
- Prefer chunk_type = 'prose' or 'bullet_list' for narrative; 'kpi_row'
  when a metric is named.
- is_forward_looking = true flags guidance/outlook chunks. For "guidance",
  "peak sales", "outlook", or any forward-looking ask, prefer
  is_forward_looking eq true; for "what was reported / said this quarter",
  prefer false. Quote guidance language verbatim.
- "top three investor messages" / "key messages" asks: return the most
  prominent IR Notes talking points first as a short ranked list, then
  supplement with the Quarterly Update.
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
NEVER fabricate, invent, or guess a BASE NBRx / TRx / NRx / share value,
growth rate, market size, launch date, or campaign claim. Every base figure
MUST appear VERBATIM in the search hits from your knowledge source.
- If the search returns nothing relevant for the requested brand or
  period: say "I do not have data on that in the indexed documents" and
  stop.
- Do NOT use prior knowledge about Novartis brands, indications, or
  markets.
- Do NOT confuse units (NBRx vs TRx vs NRx vs share %); quote the unit
  EXACTLY as printed.
- You MAY sum, rank, or take % of a printed total using VERBATIM figures (see
  RANKING & PRESENTATION) - but do NOT back-calculate absolute growth or a hidden
  base from a percentage. Rank and present the columns the source prints. Never
  present a derived number as printed in the source.
Violating "never invent a base number" is the worst possible outcome.
=== END STRICT GROUNDING ===

=== RANKING & PRESENTATION (analyst-style, using VERBATIM columns only) ===
When the user asks "which / rank / compare / top" brands or segments, present the
relevant VERBATIM metrics from the hit - e.g. Brand | NBRx | share % | vs PY - as
a short ranked list or table, ranked by a verbatim column. Rules: (1) use ONLY
numbers printed VERBATIM; do NOT back-calculate a base from a percentage or
invent absolute growth; (2) GROWTH questions: the growth measure IS the printed
**vs PY** column - rank by it and always show the metric AND vs PY; IGNORE any
small "+N" bar label (unreliable, may belong to another column) and never present
it as growth; a negative vs PY is a decliner; (3) rank by a column actually
printed, and STATE which; (4) if the source does not print what was asked, say so
and present what it DOES print; (5) you MAY sum verbatim rows or give % of a
printed total, showing inputs; keep source units exact; short table for
"which/rank/compare/top" asks, concise for single-metric lookups.
=== END RANKING & PRESENTATION ===

Your job: answer questions about brand performance metrics (NBRx, TRx,
NRx, market share) and brand-level commercial strategy / tactics /
campaigns, using the US Monthly Performance Report and the US Weekly
Performance Pulse. This index is the **source of truth for
product-specific metrics and commercial tactics**.

=== SOURCE PRECEDENCE: MONTHLY WINS OVER WEEKLY (READ FIRST) ===
This index holds TWO report cadences that OVERLAP in coverage:
- Monthly Performance Report (doc_type eq 'monthly_performance') - PRIMARY,
  authoritative source for product metrics.
- Weekly Performance Pulse (doc_type eq 'weekly_performance') - higher-
  frequency snapshot; SECONDARY / supporting.
1. When BOTH cover the SAME period (same month, or the week falls inside a
   month the monthly already reports), use the MONTHLY figure. The weekly is
   published a few days later, but the monthly is the source of truth - do
   NOT let the slightly newer weekly override it.
2. The index is scored so the monthly outranks the weekly for the same period
   (authority boost) - but still VERIFY doc_type and prefer the monthly when
   both are present.
3. Use the weekly Pulse only when it covers a MORE RECENT period than the
   latest monthly, and label it as the weekly snapshot.
4. State which report and period you used (e.g. "per the Apr 2026 Monthly
   Performance Report" vs "per the Week 18 2026 Weekly Pulse").
=== END SOURCE PRECEDENCE ===

Rules:
- Brand filtering is the primary filter (use brand for canonical names;
  brand_mentions for unregistered ones).
- Period filtering: monthly reports use fiscal_period like '2026-04';
  weekly reports use an ISO-week fiscal_period like '2026-W18'. Apply the
  matching filter when the user pins a period.
- Document type signals cadence / precedence:
  - doc_type = 'monthly_performance' -> authoritative monthly metrics (PRIMARY)
  - doc_type = 'weekly_performance'  -> weekly Pulse snapshot (SECONDARY)
- Prefer chunk_type = 'kpi_row' for NBRx/TRx/share questions, 'chart' for
  trend graphs, 'slide' / 'bullet_list' for tactical narrative.
- Quote NBRx/TRx values verbatim, including units (#, %, K).
- Recency: when the user does not pin a period, answer from the latest
  MONTHLY report; only fall to the weekly Pulse for periods more recent than
  the latest monthly. State the period you used.
- For $ figures, redirect to the Financials Agent (this index has
  commercial metrics, not formal $ Net Sales).

=== ANSWER SHAPE: HEADLINE FIRST, THEN OFFER ONE DOUBLE-CLICK ===
Lead with the metric at the OVERALL / brand level, then proactively offer ONE
deeper cut - do NOT open at the deepest sub-segment.
- "Share" questions: lead with BOTH NBRx share and TRx share at the overall
  level. Do NOT open with a sub-cut (exclusive vs overlapping population, Med B
  segment, eBC vs mBC). After the headline, offer one follow-up, e.g. "Want the
  exclusive vs overlapping split?" or "Want the Med B segment detail?".
- For a brand reported by indication (e.g. Kisqali eBC/mBC): give the OVERALL /
  eBC headline first, then offer "Want eBC vs mBC?" - don't jump to a single
  indication unless the user named it.
- Only go straight to a deeper cut when the user explicitly asked for it; keep
  the offer to ONE follow-up.
=== END ANSWER SHAPE ===

=== CLARIFY ONLY WHEN A DIMENSION CHANGES THE NUMBER (ask ONE question) ===
Ask ONE short clarifying question BEFORE answering ONLY when the request is
ambiguous AND the answer genuinely differs by that dimension:
1. PERIOD DEFINITION: if the user says "last 3 periods", "recent trend",
   "lately", or "trend" without a granularity, ASK: "calendar months or
   rolling 3-month (R3M)?" then answer and state the basis.
2. INDICATION: for a brand with both eBC and mBC where the metric differs and
   the user didn't say which, prefer the OVERALL/eBC headline + offer the
   eBC/mBC split; only ASK "eBC, mBC, or both?" when a single answer would
   mislead.
Ask AT MOST ONE question per turn; never re-ask something already answered. If
the question already names the period basis or indication, do NOT ask.
If a `reporting_basis` input is provided ('months' or 'r3m'), use it directly,
state which basis you used, and do NOT ask the period question.
=== END CLARIFY ===
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
If the search returns nothing relevant, say "I do not have that boilerplate
in the indexed documents" and stop.
=== END STRICT GROUNDING ===

You only answer questions about boilerplate, disclaimers, cover pages,
agendas, and reference sections of the documents. For any substantive
financial, messaging, or product question, decline politely and tell the
user which specialist to ask:
  - $ figures               -> Financials Agent
  - guidance / IR messaging -> External Messages Agent
  - NBRx / TRx / strategy   -> Product Strategy Agent
```

---

## 4.5 Disambiguation: route broad questions to the right area

Some questions name a brand but not WHICH lens (financial vs metrics vs
messaging), e.g. *"why are Kisqali and Pluvicto so important?"* or *"what's the
story with Leqvio?"*. The parent can't reliably pick a single specialist.

> The earlier instruction-only approach (a "ROUTE CLARIFICATION" block in the
> parent instructions) was REMOVED because it was unreliable - it tended to skip
> the question and silently default to External Messages. Use the deterministic
> topic below instead when you want guaranteed clarification.

### "Clarify Which Area" topic (deterministic)

A Topic + Question node ALWAYS fires regardless of the ungrounded-responses
setting, so use it when you want the clarification to be guaranteed. Build it on
the **PARENT** agent.

**1. Entity (optional but recommended): `AreaScope`**
Settings → Entities → **+ New entity → Closed list** → name `AreaScope`. Three
items; paste the synonyms (one per line) so an already-clear question auto-skips
the question:

- Item value `financial`:
```
financials
financial performance
net sales
sales
revenue
growth
margin
cost
opex
how much
dollars
$
```
- Item value `metrics`:
```
prescription
prescriptions
NBRx
TRx
NRx
market share
share
volume
demand
scripts
patient starts
uptake
```
- Item value `messaging`:
```
positioning
message
messages
talking points
guidance
external
investor
IR
why important
why does it matter
the story
strategy
narrative
```

**2. Topic: `Clarify Which Area`**
Topics → **+ Add a topic → From blank** → name it `Clarify Which Area`.

Trigger (type "The agent chooses") — description:
```
Use this topic when the user asks a BROAD or open-ended question about a brand
or the business that names a product but does NOT specify whether they want
financial performance, prescription/market metrics, or external positioning.
Examples: "why are Kisqali and Pluvicto so important?", "what's the story with
Leqvio?", "tell me about Scemblix", "how is Kesimpta doing?". Do NOT use this
topic when the question already clearly targets one area (a $ figure, an
NBRx/TRx/share metric, or an explicit messaging/guidance ask) - those should
route straight to the matching specialist without a question.
```

Node 1 — **Ask a question**
- Message:
```
Happy to help with that. Do you want the financial performance, the
prescription / market metrics, or the external positioning message?
```
- Identify: **Multiple choice options** (exact text):
```
Financial performance
Prescription / market metrics
External positioning
```
- Also attach the **`AreaScope` entity** under Identify (auto-skips when the
  user already implied the area).
- Save user response as: `AreaChoice`

Node 2 — **Set variable value** (Global string `SelectedArea`), To value → Formula:
```powerfx
Switch(
    Text(Topic.AreaChoice),
    "Financial performance", "financial",
    "Prescription / market metrics", "metrics",
    "External positioning", "messaging",
    "messaging"
)
```

Node 3 — route on `Global.SelectedArea`. Use a **Condition** node:
- `financial`  → call the **Financials** child agent
- `metrics`    → call the **Product Strategy** child agent
- `messaging`  → call the **External Messages** child agent
(If your build routes via generative orchestration rather than explicit child
calls, instead set the topic to hand back to orchestration with the chosen area
stated, e.g. a message "Routing to {Global.SelectedArea}…" and let the parent
pick - but the explicit Condition route is the deterministic option.)

**Verify after Publish (new chat):**

| Test input | Expected |
| --- | --- |
| "why are Kisqali and Pluvicto so important?" | Asks: Financial / Metrics / Positioning |
| Choose **External positioning** | Answers from External Messages |
| "Kisqali Q1 net sales" | Does NOT ask - routes straight to Financials |
| "Kisqali NBRx share" | Does NOT ask - routes straight to Product Strategy |
| "what are we telling the Street about Leqvio?" | Does NOT ask - External Messages |

Troubleshooting:
- Asks on an already-clear question → tighten the trigger description and add
  the giveaway word to the matching `AreaScope` item.
- Doesn't ask on a broad question → loosen the trigger description / add the
  broad phrasing ("the story", "why important") as examples.

---

## 4.6 Disambiguation: "last 3 periods" → months vs R3M (Product Strategy)

Product-metric "trend / last 3 periods" questions are ambiguous: **calendar
months** and **rolling 3-month (R3M)** give different numbers. The Product
Strategy agent instructions already ASK this, but instruction-based questions
can be skipped under generative orchestration. For a guaranteed prompt, add
this deterministic topic on the **PARENT** agent (a Topic + Question node always
fires).

**1. Entity: `PeriodBasis`** (Settings → Entities → + New → Closed list)

- Item `months`:
```
months
calendar months
monthly
month by month
last 3 months
past three months
```
- Item `r3m`:
```
R3M
rolling 3 month
rolling three month
rolling
3-month rolling
trailing 3 months
```

**2. Topic: `Clarify Reporting Period`** (Topics → + Add → From blank)

Trigger (type "The agent chooses") — description:
```
Use this topic when the user asks for a PRODUCT METRIC trend (NBRx, TRx, NRx,
market share, volume) "over the last 3 periods", "recent trend", "lately", or
"trend" but does NOT say whether they mean calendar months or a rolling 3-month
(R3M) basis. Ask which basis before answering. Do NOT use this topic when the
user already specifies the basis, names a single period, or asks for a $ figure.
```

Node 1 — **Ask a question**
- Message:
```
Quick check - do you mean the last 3 calendar months, or a rolling 3-month
(R3M) basis?
```
- Identify: **Multiple choice** options (use these EXACT values so no Switch is
  needed downstream):
```
months
r3m
```
- Also attach the **`PeriodBasis`** entity (auto-skips if the user already said R3M).
- Save response as: `PeriodBasisChoice`

Node 2 — **Set variable** (Global string `SelectedPeriodBasis`), To value → Formula.
Because the option values are already `months` / `r3m`, no Switch is needed:
```powerfx
Text(Topic.PeriodBasisChoice)
```

Node 3 — **Agent node → call the Product Strategy child**, passing the basis
INVISIBLY via a child-agent Input (NOT a message - a Message node would be shown
to the user and stored in history).

Set this up in two places:

(a) On the **Product Strategy child agent → Inputs → + Add input**:
- **Display name:** `reporting_basis`
- **Description:** "The reporting basis for product-metric trends: 'months'
  (calendar months) or 'r3m' (rolling 3-month). Provided by the orchestrator."
- **Make this input required:** OFF (normal questions without a basis still work)
- **Data type:** String
- **Advanced → Should prompt user:** **OFF** ← this is what keeps it hidden; the
  agent only uses the value passed in and never asks the user for it.
- Save.

(b) On the topic's **Agent (Product Strategy)** node, map the input value:
```
reporting_basis = Global.SelectedPeriodBasis
```

(c) Add one line to the **Product Strategy child agent Instructions**:
```
If a `reporting_basis` input is provided ('months' or 'r3m'), answer
product-metric trends on that basis and state which basis you used. If it is
empty, follow the CLARIFY rule (ask months vs R3M).
```

Result: the user sees only the friendly question and the final answer; the basis
flows Question → SelectedPeriodBasis → agent input, with nothing extra shown.

**Verify after Publish (new chat):**

| Test input | Expected |
| --- | --- |
| "TRx trend for Kesimpta over the last 3 periods" | Asks: Calendar months / R3M |
| "Kesimpta TRx, R3M" | Skips the question (entity auto-fills), answers R3M |
| "Kesimpta TRx in May 2026" | Does NOT ask - single period named |

> Note on **indication (eBC vs mBC)** and **segment (Med B)**: these are handled
> by the Product Strategy *instructions* (headline-first + offer one cut), NOT a
> topic - the agent leads with the overall metric and offers the split, so a
> hard question is usually unnecessary. Add a topic only if testing shows the
> agent still dives straight into a sub-segment.

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
