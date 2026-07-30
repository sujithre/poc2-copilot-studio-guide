"""Segment discovery: mine sub-population descriptors from the KPI sidecars.

Pipeline position: run AFTER chunker.py, BEFORE index_upload.py (same slot as
discover_brands.py).

Why this exists
---------------
A share/growth number is only interpretable together with the population it is
measured over. The same brand in the same deck can legitimately show
`share -0.7pts` for one audience and `share +1.9pts` for the total market. If the
pipeline cannot tell those apart, retrieval and the answering model will present
whichever one happens to match the question's wording.

Rather than hardcode a list of segments (which would overfit to today's deck and
today's brands), this script DISCOVERS the vocabulary from the corpus, the way
discover_brands.py discovers brands. A human confirms the candidates and pastes
them into `manifest.segment_registry.segments` (same shape as
`manifest.brand_registry.brands`); chunker.py then stamps every chunk with
`segment_level` (total | subsegment) and `segment_name`.

Where candidates come from
--------------------------
The KPI sidecars written by chunker.py (`kpi/<doc_id>.kpi.json`) carry the
STRUCTURED descriptor fields that say what a number measures:

    basis   e.g. "MS market", "B-cell", "YTD NBRx share"
    scope   e.g. "US", "MS Market"
    name    e.g. "Kesimpta share with generalists in B-cell"

Those fields are the signal. Rendered chunk text is deliberately NOT used: it
contains the serialized field names and the scope banner, which swamp the output
with words like "period", "basis", "comparison" and "single-month".

Table and figure captions (chunk.section) are mined too, since tabular and
graphical cuts name their population there.

Outputs
-------
  kpi/segment_candidates.json   (human review file; NOT uploaded to the index)

    descriptor_values  distinct field/value pairs with counts - the primary
                       review surface, because a whole descriptor such as
                       "MS market" or "share with generalists" is far easier to
                       judge than a bare token.
    token_candidates   residual single tokens after metric/period/geo/brand and
                       stop-word vocabulary is removed - a secondary net for
                       populations that never appear as a clean descriptor.

Usage
-----
  python discover_segments.py
  python discover_segments.py --only product_strategy
  python discover_segments.py --only product_strategy --min-mentions 3
"""
from __future__ import annotations
import argparse
import json
import re
from collections import defaultdict

from common import load_manifest, paths

# --- Vocabulary that describes the MEASURE, the PERIOD or the GEOGRAPHY.
# None of these name a population, so they are stripped before a token candidate
# is proposed.
_MEASURE_WORDS = {
    "nbrx", "trx", "nrx", "share", "shares", "sales", "net", "gross", "price",
    "volume", "mix", "growth", "market", "demand", "units", "unit", "inventory",
    "adherence", "value", "values", "delta", "pts", "pt", "percent", "pct",
    "rate", "level", "levels", "total", "attainment", "status", "target", "tgt",
    "budget", "actual", "actuals", "act", "ambition", "outlook", "lo", "plan",
    "contribution", "vs", "versus", "change", "changes", "count", "avg",
    "average", "figure", "figures", "chart", "charts", "table", "tables",
    "metric", "metrics", "kpi", "kpis", "performance", "summary", "overview",
    "dollars", "usd", "bnusd", "eur", "sit", "doh", "gtn", "pvm",
    # planning / commentary vocabulary seen across the real corpus
    "peak", "impact", "timing", "top", "size", "bridge", "estimated", "expected",
    "guidance", "goal", "goals", "risk", "risks", "major", "minor", "underlying",
    "key", "core", "driver", "drivers", "penetration", "reach", "coverage",
    "execution", "corporate", "commercial", "business", "launch", "study",
    "studies", "trial", "trials", "category", "categories", "tier", "tiers",
    "fte", "ftes", "nocc", "opex", "cost", "costs", "margin",
}
_PERIOD_WORDS = {
    "ytd", "mtd", "qtd", "r3m", "r4w", "rolling", "trailing", "month", "monthly",
    "months", "quarter", "quarterly", "quarter-to-date", "year-to-date",
    "single-month", "week", "weekly", "annual", "year", "years", "yearly", "yoy",
    "py", "prior", "previous", "current", "exit", "calendar", "date", "dated",
    "period", "periods", "through", "reported", "basis", "scope", "measure",
    "comparison", "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
    "sept", "oct", "nov", "dec", "january", "february", "march", "april", "june",
    "july", "august", "september", "october", "november", "december",
    "fy", "q1", "q2", "q3", "q4", "h1", "h2",
}
_GEO_WORDS = {
    "us", "usa", "ex-us", "global", "ww", "worldwide", "europe", "eu",
    "international", "geography", "region", "regional", "country", "local",
}
# Ordinary English function words that survive tokenization of a descriptor.
_STOP_WORDS = {
    "a", "an", "and", "or", "the", "of", "for", "in", "on", "at", "to", "from",
    "by", "with", "without", "per", "as", "is", "are", "was", "were", "be",
    "been", "it", "its", "this", "that", "these", "those", "one", "two", "three",
    "only", "also", "all", "any", "each", "both", "other", "others", "new",
    "brand", "brands", "product", "products", "name", "names", "id", "type",
    "not", "no", "yes", "na", "tbd", "unknown", "none",
}
_NOISE = _MEASURE_WORDS | _PERIOD_WORDS | _GEO_WORDS | _STOP_WORDS

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-/+']*", re.IGNORECASE)
_YEARISH_RE = re.compile(r"^[’'`]?\d{2,4}$")
_NUMERIC_RE = re.compile(r"^[\d.,%+\-]+$")


def _informative_tokens(text: str) -> list[str]:
    """Population-ish tokens: drop measure/period/geo/stop vocabulary and numbers."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        t = raw.strip("-/+'").lower()
        if not t or len(t) < 2 or t in _NOISE:
            continue
        if _YEARISH_RE.match(t) or _NUMERIC_RE.match(t):
            continue
        out.append(t)
    return out


def _registry_entries(manifest: dict, registry_key: str, list_key: str) -> list[dict]:
    """Entries from a manifest registry, matching the brand_registry convention:

        "<registry_key>": {"<list_key>": [{"canonical": ..., "aliases": [...]}, ...]}
    """
    block = (manifest or {}).get(registry_key) or {}
    entries = block.get(list_key, []) or []
    return [e for e in entries if isinstance(e, dict)]


def _known_aliases(manifest: dict) -> set[str]:
    known: set[str] = set()
    for entry in _registry_entries(manifest, "segment_registry", "segments"):
        canonical = (entry.get("canonical") or "").strip()
        if canonical:
            known.add(canonical.lower())
        for a in entry.get("aliases") or []:
            known.add(str(a).strip().lower())
    return known


def _brand_tokens(manifest: dict, kpi_dir) -> set[str]:
    """Every token that is (or might be) a BRAND name - never a population.

    Sources: manifest.brand_registry.brands, documents[*].brand (BrandRegistry
    auto-extends the same way), and whatever discover_brands.py already proposed
    in kpi/brand_candidates.json. A fresh corpus surfaces hundreds of
    unregistered competitor brands, and an unregistered brand name looks exactly
    like an unregistered population descriptor.
    """
    known: set[str] = set()

    def add(text) -> None:
        for tok in str(text or "").split():
            t = tok.strip("-/+,.()'").lower()
            if t:
                known.add(t)

    for entry in _registry_entries(manifest, "brand_registry", "brands"):
        add(entry.get("canonical") or "")
        for a in entry.get("aliases") or []:
            add(a)
    for doc in (manifest or {}).get("documents", []) or []:
        add(doc.get("brand") or "")

    path = kpi_dir / "brand_candidates.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        items = data.get("candidates") if isinstance(data, dict) else data
        for item in items or []:
            if isinstance(item, dict):
                # discover_brands.py writes the key as "token".
                add(item.get("token") or item.get("candidate") or item.get("name") or "")
            else:
                add(item)
    return known


def _doc_index_map(manifest: dict) -> dict[str, str]:
    return {
        d.get("doc_id", ""): d.get("primary_index", "")
        for d in (manifest or {}).get("documents", []) or []
        if d.get("doc_id")
    }


def _iter_jsonl(path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-mentions", type=int, default=2)
    ap.add_argument("--only", default=None, help="Restrict to one logical index")
    ap.add_argument("--top", type=int, default=40, help="Rows to print per section")
    args = ap.parse_args()

    manifest = load_manifest()
    p = paths()
    known = _known_aliases(manifest)
    brand_tokens = _brand_tokens(manifest, p["kpi"])
    doc_index = _doc_index_map(manifest)

    index_names = list(manifest["indices"])
    if args.only:
        if args.only not in index_names:
            print(f"Unknown index '{args.only}'. Choose from: {index_names}")
            return 2
        index_names = [args.only]
    else:
        print("WARNING: no --only given, so every index is mined at once. Finance and")
        print("         external-messaging descriptors will dominate the ranking and bury")
        print("         the brand-performance populations. Prefer:")
        print("           python discover_segments.py --only product_strategy")
    wanted_docs = {d for d, idx in doc_index.items() if idx in index_names}

    print(f"indices   : {index_names}")
    print(f"documents : {len(wanted_docs)}")
    print(f"excluding : {len(brand_tokens)} brand tokens, {len(known)} registered segment aliases")

    values: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "docs": set(), "samples": []}
    )
    token_mentions: dict[str, int] = defaultdict(int)
    token_docs: dict[str, set] = defaultdict(set)
    token_samples: dict[str, list] = defaultdict(list)

    def record(field: str, value: str, doc_id: str, sample: str) -> None:
        v = (value or "").strip()
        if not v:
            return
        rec = values[(field, v)]
        rec["count"] += 1
        rec["docs"].add(doc_id)
        if len(rec["samples"]) < 3 and sample:
            rec["samples"].append(sample[:160])
        for tok in _informative_tokens(v):
            if tok in known or tok in brand_tokens:
                continue
            token_mentions[tok] += 1
            token_docs[tok].add(doc_id)
            if len(token_samples[tok]) < 3:
                token_samples[tok].append(f"{field}={v}"[:160])

    kpi_files = sorted(p["kpi"].glob("*.kpi.json"))
    for fp in kpi_files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        doc_id = data.get("doc_id", "")
        if wanted_docs and doc_id not in wanted_docs:
            continue
        for kp in data.get("kpis") or []:
            label = kp.get("name", "") or ""
            record("basis", kp.get("basis", ""), doc_id, label)
            record("scope", kp.get("scope", ""), doc_id, label)
            record("name", label, doc_id, kp.get("source_quote", ""))

    # Table / figure captions carry the population for tabular & graphical cuts.
    for name in index_names:
        for rec in _iter_jsonl(p["chunks"] / f"{name}.jsonl"):
            if rec.get("chunk_type") in ("table", "table_row", "chart", "figure"):
                record("caption", rec.get("section", ""), rec.get("doc_id", ""), "")

    descriptor_values = sorted(
        (
            {
                "field": field,
                "value": value,
                "count": info["count"],
                "docs": sorted(d for d in info["docs"] if d),
                "sample_contexts": info["samples"],
                "confirmed": False,
            }
            for (field, value), info in values.items()
            if info["count"] >= args.min_mentions and field != "name"
        ),
        key=lambda r: (-r["count"], r["field"], r["value"].lower()),
    )
    token_candidates = sorted(
        (
            {
                "candidate": tok,
                "mentions": n,
                "docs": sorted(d for d in token_docs[tok] if d),
                "sample_contexts": token_samples[tok],
                "suggested_kind": "",
                "confirmed": False,
            }
            for tok, n in token_mentions.items()
            if n >= args.min_mentions
        ),
        key=lambda r: (-r["mentions"], r["candidate"]),
    )

    out_dir = p["kpi"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "segment_candidates.json"
    out_path.write_text(
        json.dumps(
            {
                "how_to_use": (
                    "Look for values that name a POPULATION the metric is measured over "
                    "(an HCP audience, a line of therapy, an indication, a channel, a "
                    "sub-market). Add confirmed ones to manifest.segment_registry.segments "
                    'as {"canonical": "...", "aliases": [...], "kind": '
                    '"audience|line|indication|channel|market"} then re-run chunker.py. '
                    "Ignore anything that describes a metric, a period or a brand. "
                    "`descriptor_values` is the primary review surface; "
                    "`token_candidates` is a secondary net."
                ),
                "indices": index_names,
                "min_mentions": args.min_mentions,
                "already_registered": sorted(known),
                "descriptor_values": descriptor_values,
                "token_candidates": token_candidates,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {out_path}")
    print(f"  {len(descriptor_values)} descriptor values, "
          f"{len(token_candidates)} token candidates (>= {args.min_mentions} mentions)")
    if not kpi_files:
        print("  NOTE: no kpi/*.kpi.json sidecars found - run chunker.py first.")

    print("\nDistinct descriptor values (field | count | value) - review these first:")
    for r in descriptor_values[: args.top]:
        print(f"  {r['field']:<8} {r['count']:>5}  {r['value'][:70]}")

    print("\nResidual tokens:")
    for r in token_candidates[: args.top]:
        print(f"  {r['mentions']:>5}  {r['candidate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
