# POC2 — FinSight US

Mirror of the original Novartis pipeline, configured for 7 US documents across
3 source-of-truth domains.

## Layout

```
POC2/
  manifest.json          # FinSight US indices, docs, taxonomies, brand registry
  docs/                  # source PDFs (convert .pptx / .docx -> PDF first)
  PDF_pages/<doc_id>/    # rasterized PNGs (cached by content hash)
  PDF_vision/<doc_id>/   # per-page vision JSON (cached)
  chunks/<index>.jsonl   # one file per logical index
  kpi/<doc_id>.kpi.json  # KPI sidecar per doc
  pipeline/              # scripts (run from inside this folder)
```

## Indices (Azure)

| Logical             | Azure index                       | Source-of-truth for                        |
|---------------------|-----------------------------------|--------------------------------------------|
| financial_results   | finsight-us-financial-results     | Net Sales, Cost, margin, OPEX              |
| external_messages   | finsight-us-external-messages     | IR messaging, guidance, earnings narrative |
| product_strategy    | finsight-us-product-strategy      | NBRx, TRx, brand tactics, campaigns        |
| meta                | finsight-us-meta                  | covers, disclaimers, agendas, references   |

## Env

Reuses the workspace `.env` at `C:\Projects\Bio\.env`. Add a `POC2/.env` only
if you want POC2-specific overrides (it wins over the workspace one).

Required env vars (already set for the original pipeline, so nothing to change):

- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_VERSION`
- `AZURE_OPENAI_VISION_DEPLOYMENT`
- `AZURE_OPENAI_EMBED_DEPLOYMENT`
- `AZURE_SEARCH_ENDPOINT`

Optional:

- `RASTER_DPI` (default 250)
- `VISION_PROMPT_VERSION` (default `v1`)
- `VISION_MAX_CONCURRENCY` (default 4)
- `EMBED_BATCH_SIZE` (default 16)

## Run order (from `POC2/pipeline/`)

```powershell
# 0. Activate the workspace venv (shared with the original pipeline)
& C:\Projects\Bio\.venv\Scripts\Activate.ps1
cd C:\Projects\Bio\POC2\pipeline

# 1. Drop the 7 PDFs into POC2/docs/ matching the names in manifest.json:
#    docs/2026-03 - US Results.pptx                       -> convert to PDF
#    docs/IR notes 2025Q4.docx                            -> convert to PDF
#    docs/US Quarterly Update Q1 2026 vF.docx             -> convert to PDF
#    docs/03.23.2026 Leqvio MBR B Ready. Scale Steady. Campaign Plan 2026.pdf
#    docs/03.23.2026 Pluvicto MBR Pre Read.pdf
#    docs/Cosentyx 2025-26 IPS Cross-Functional Strategy Pre-Read.pdf
#    docs/LRR1_Voto_ PREREADS.pdf

# 2. Rasterize PDFs to page PNGs
python rasterize.py

# 3. Run vision extraction (LLM) -> per-page JSON
python vision_extract.py

# 4. Inspect a single doc's vision output (sanity check)
python inspect_vision.py us-results-2026-03

# 5. Chunk + write KPI sidecars
python chunker.py

# 5b. (optional) Discover brand candidates not yet in the registry.
#     Reviews kpi/brand_candidates.json; you can add real ones to manifest
#     and re-run chunker.py before upload. --llm adds yes/no/unsure judgement.
python discover_brands.py --llm

# 6. Create / update Azure Search indices (idempotent)
python index_create.py

# 7. Embed and upload chunks
python index_upload.py

# 8. Smoke-test retrieval
python smoke_query.py "How is Pluvicto Q1 2026 doing?" --index external_messages
python smoke_query.py "March 2026 Net Sales by brand"   --index financial_results
python smoke_query.py "Leqvio campaign plan"            --index product_strategy

# 9. Provision Foundry agents (one per index) + supervisor
python agents/create_agents.py
python agents/orchestrator.py "How much did Leqvio grow in Q1 vs PY?"
python agents/orchestrator.py --devui     # browser DevUI
```

## Agents

Four specialist Foundry agents — one per index — plus a supervisor that
fans out to them.

| Agent name (default prefix) | Backed by index | Purpose |
|---|---|---|
| `finsight-us-financials` | `finsight-us-financial-results` | $ figures: Net Sales, Cost, Margin, OPEX |
| `finsight-us-external`   | `finsight-us-external-messages` | IR messaging, guidance, Q&A |
| `finsight-us-product`    | `finsight-us-product-strategy`  | NBRx, TRx, brand tactics, LRR |
| `finsight-us-meta`       | `finsight-us-meta`              | Boilerplate, disclaimers, agendas |

The **supervisor** (`agents/orchestrator.py`) classifies intent and calls
one or more specialists. Multi-domain questions (e.g. *"How is Leqvio
doing?"*) fan out to financials + product + external in parallel and the
supervisor synthesizes a single cited answer.

### Why a `meta` agent?

The meta index holds covers / disclaimers / agenda pages. Its agent is
intentionally minimal — its job is to answer "what does the cover say"
and to **deflect substantive questions** back to the right specialist.
Without it, the supervisor might route boilerplate questions to the
wrong domain agent and confuse it. With it, the routing rules stay
clean: meta in, meta out.

You will rarely call this agent directly — the supervisor sends questions
there only when explicitly about boilerplate.

### Required Foundry env vars (in addition to the pipeline ones)

- `AZURE_AI_PROJECT_ENDPOINT` — Foundry project endpoint
- `FOUNDRY_MODEL_DEPLOYMENT` — model name (e.g. `gpt-4.1`)
- `FOUNDRY_SEARCH_CONNECTION_ID` — AI Search project connection (name or ARM id)
- `FOUNDRY_AGENT_PREFIX` — default `finsight-us`

### Re-provisioning

```powershell
python agents/delete_agents.py     # remove all 4 agents
python agents/create_agents.py     # re-create with current specs
```

## Routing (one agent per index)

Each index has its own agent. The supervisor (see `agents/orchestrator.py`)
routes questions based on intent and fans out to multiple specialists for
multi-domain questions. There is no KPI reconciliation across indices.

```
question -> intent classifier -> route_to (manifest.routing_hints.rules)
                                  |
                                  +-- financial_results agent
                                  +-- external_messages agent
                                  +-- product_strategy  agent
                                  +-- meta               agent
```

## Brand registry + discovery (3-tier)

Filtering on `brand` requires a canonical, controlled vocabulary; auto-adding
LLM-extracted strings would pollute the field with typos, person names, and
duplicates. POC2 uses a 3-tier approach:

| Tier | Where it lives | Purpose |
|---|---|---|
| 1. **Registry** | `manifest.brand_registry.brands` | Closed vocabulary. Source of truth for the `brand` filter field. ~20-30 strategic brands. Manually curated. |
| 2. **Discovery** | `discover_brands.py` -> `kpi/brand_candidates.json` | Scans chunks for unregistered brand-like tokens, optionally LLM-validates, writes a review file. Never modifies the index. |
| 3. **Mentions** | Chunk field `brand_mentions` (Azure Search filterable Collection) | Lossless capture of every brand-like name (canonical + raw unknowns). Filterable today, even before promotion. |

### How chunks carry brands

Each chunk has two brand fields:

| Field | Source | Use case |
|---|---|---|
| `brand` | Registry canonical names only | Strict filtering, UI dropdowns, agent tools, dashboards |
| `brand_mentions` | Canonical + raw unknown names from vision + headings | Catches the long tail; filterable on exact text match |

Three independent ways to find a drug like `Pormact` in the index:

```text
1. $search=Pormact                                     (full-text + vector hybrid)
2. $filter=brand_mentions/any(b: b eq 'Pormact')       (works for unregistered brands)
3. $filter=brand/any(b: b eq 'Pormact')                (only after promotion to registry)
```

### Promoting a candidate to canonical

```text
1. python chunker.py                       # produces chunks/*.jsonl
2. python discover_brands.py --llm         # writes kpi/brand_candidates.json
3. open kpi/brand_candidates.json, pick the real ones
4. add entries to manifest.brand_registry.brands:
     { "canonical": "Pormact", "aliases": ["Pormact", "PORMACT"],
       "therapeutic_area": "oncology" }
5. python chunker.py                       # re-chunk; Pormact now has brand=["Pormact"]
6. python index_upload.py                  # push updated chunks
```

### Why not auto-add to manifest?

- Typos become permanent canonical brands (`Cosentvx`)
- Person/place names get registered (`Bradley`, `Chicago`)
- No therapeutic-area mapping for auto-adds, breaking TA filters
- Aliases get fragmented (`Pormact` and `PORMACT` as two brands)
- A 5-minute quarterly review of `brand_candidates.json` is cheaper than
  building the false-positive filter that would replace it

## Section taxonomy (IR notes)

`manifest.section_taxonomies.ir_notes` declares the Part 0..Part 4 structure.
The chunker:

1. Walks all pages of an `ir_notes` doc and builds a `page_no -> part_id` map
   by detecting `## Part N`-style headings. State carries forward across pages
   that have no heading (so page 6's CRM commentary inherits page 5's Part 2).
2. Splits each page's markdown by `## Heading` boundaries into sections.
3. For each section, if the heading matches a brand alias in
   `manifest.brand_registry`, the chunk's `brand` is overridden to that
   canonical name and `therapeutic_area` is inferred from the brand.
4. `section_path` is set to `[Part name, Drug name]` (or `[Part name]` for
   between-drug commentary, or `[Heading]` for preamble pages).
5. Each section's text is auto-classified into `prose` / `bullet_list` /
   `quote` by `classify_text_style` (applied for all doc types, not just IR
   notes).

Disambiguation: `Part 2 Immunology` vs `Part 2 Cardio-Renal Metabolic` is
resolved by longest-phrase match (the TA name beats the bare `Part 2`).
Heading-like filtering rejects bullet items, quoted lines, and full sentences
so body text is never mistaken for a Part header.

## Quick validation (no PDFs needed)

```powershell
cd C:\Projects\Bio\POC2\pipeline
python _smoke_helpers.py    # brand registry + Part detector + style classifier
python _smoke_chunker.py    # synthetic IR notes -> chunks
```
