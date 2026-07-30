"""Segment discovery: mine sub-population descriptors from chunks/*.jsonl.

Pipeline position: run AFTER chunker.py, BEFORE index_upload.py (same slot as
discover_brands.py).

Why this exists
---------------
A share/growth number is only interpretable together with the population it is
measured over. The same brand in the same deck can legitimately show
`share -0.7pts` for one audience and `share +1.9pts` for the total market. If
the pipeline cannot tell those apart, retrieval and the answering model will
present whichever one happens to match the question's wording.

Rather than hardcode a list of segments (which would overfit to today's deck and
today's brands), this script DISCOVERS the vocabulary from the corpus, exactly
the way discover_brands.py discovers brands. A human confirms the candidates and
pastes them into `manifest.segment_registry`; chunker.py then stamps every chunk
with `segment_level` (total | subsegment) and `segment_name`.

Where candidates come from
--------------------------
KPI rows carry descriptor fields that say WHAT the number measures:
  - `basis`  e.g. "MS market", "B-cell", "YTD NBRx share"
  - `scope`  e.g. "US", "MS Market"
  - `name`   e.g. "Kesimpta share with generalists in B-cell"
Table and figure captions carry the same information for tabular/graphical cuts.

The metric/period words in those strings are noise; the population words are the
signal. We strip known metric and period vocabulary, then report what is left,
ranked by how many chunks use it.

Outputs
-------
  kpi/segment_candidates.json   (human review file; NOT uploaded to the index)

Usage
-----
  python discover_segments.py
  python discover_segments.py --min-mentions 3
  python discover_segments.py --only product_strategy
"""
from __future__ import annotations
import argparse
import json
import re
from collections import defaultdict

from common import load_manifest, paths

# Vocabulary that describes the MEASURE or the PERIOD, never the population.
# Anything matching these is stripped before a candidate is proposed.
_MEASURE_WORDS = {
    "nbrx", "trx", "nrx", "share", "sales", "net", "gross", "price", "volume",
    "mix", "growth", "market", "demand", "units", "inventory", "adherence",
    "value", "delta", "pts", "pt", "percent", "rate", "level", "total",
    "attainment", "status", "target", "tgt", "budget", "actual", "act",
    "ambition", "outlook", "lo", "plan", "contribution", "vs", "versus",
}
_PERIOD_WORDS = {
    "ytd", "mtd", "qtd", "r3m", "r4w", "rolling", "trailing", "month", "monthly",
    "quarter", "quarterly", "week", "weekly", "annual", "year", "yoy", "py",
    "prior", "previous", "current", "exit", "dec", "jan", "feb", "mar", "apr",
    "may", "jun", "jul", "aug", "sep", "oct", "nov", "fy", "q1", "q2", "q3", "q4",
}
_GEO_WORDS = {"us", "usa", "ex-us", "global", "ww", "europe", "international"}
_NOISE = _MEASURE_WORDS | _PERIOD_WORDS | _GEO_WORDS

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-/+]*", re.IGNORECASE)
_YEARISH_RE = re.compile(r"^[’'`]?\d{2,4}$")


def _informative_tokens(text: str) -> list[str]:
    """Population-ish tokens: drop measure/period/geo vocabulary and bare numbers."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        t = raw.strip("-/+").lower()
        if not t or t in _NOISE or _YEARISH_RE.match(t):
            continue
        if t.replace(".", "").replace("%", "").isdigit():
            continue
        out.append(t)
    return out


def _known_aliases(manifest: dict) -> set[str]:
    known: set[str] = set()
    for canonical, spec in ((manifest or {}).get("segment_registry") or {}).items():
        known.add(canonical.strip().lower())
        for a in (spec or {}).get("aliases") or []:
            known.add(str(a).strip().lower())
    return known


def _brand_aliases(manifest: dict) -> set[str]:
    """Brand names are entities, not populations - never propose them as segments."""
    known: set[str] = set()
    for canonical, spec in ((manifest or {}).get("brand_registry") or {}).items():
        known.add(canonical.strip().lower())
        for a in (spec or {}).get("aliases") or []:
            known.add(str(a).strip().lower())
    return known


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-mentions", type=int, default=2)
    ap.add_argument("--only", default=None, help="Restrict to one logical index")
    args = ap.parse_args()

    manifest = load_manifest()
    p = paths()
    chunks_dir = p["chunks"]
    known = _known_aliases(manifest)
    brands = _brand_aliases(manifest)

    index_names = list(manifest["indices"])
    if args.only:
        if args.only not in index_names:
            print(f"Unknown index '{args.only}'. Choose from: {index_names}")
            return 2
        index_names = [args.only]

    mentions: dict[str, int] = defaultdict(int)
    docs: dict[str, set[str]] = defaultdict(set)
    samples: dict[str, list[str]] = defaultdict(list)

    for name in index_names:
        path = chunks_dir / f"{name}.jsonl"
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                # Only descriptor surfaces - never free narrative text, which
                # mentions many populations without being scoped to any of them.
                descriptors = [rec.get("section", "")]
                if rec.get("chunk_type") == "kpi_row":
                    descriptors.append(rec.get("text", "")[:300])
                for d in descriptors:
                    for tok in _informative_tokens(d):
                        if tok in known or tok in brands:
                            continue
                        mentions[tok] += 1
                        docs[tok].add(rec.get("doc_id", ""))
                        if len(samples[tok]) < 3 and d.strip():
                            samples[tok].append(d.strip()[:200])

    candidates = [
        {
            "candidate": tok,
            "mentions": n,
            "docs": sorted(d for d in docs[tok] if d),
            "sample_contexts": samples[tok],
            "suggested_kind": "",   # audience | line | indication | channel | market
            "confirmed": False,
        }
        for tok, n in sorted(mentions.items(), key=lambda kv: -kv[1])
        if n >= args.min_mentions
    ]

    out_dir = p["kpi"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "segment_candidates.json"
    out_path.write_text(
        json.dumps(
            {
                "how_to_use": (
                    "Review `candidate` values that name a POPULATION the metric is "
                    "measured over (an HCP audience, a line of therapy, an indication, "
                    "a channel, a market/sub-market). Add confirmed ones to "
                    "manifest.segment_registry as "
                    '{"<canonical>": {"aliases": [...], "kind": "audience|line|indication|channel|market"}} '
                    "then re-run chunker.py. Ignore candidates that describe a metric, "
                    "a period, or a brand."
                ),
                "already_registered": sorted(known),
                "candidates": candidates,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"wrote {out_path}  ({len(candidates)} candidates >= {args.min_mentions} mentions)")
    for c in candidates[:25]:
        print(f"  {c['mentions']:>5}  {c['candidate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
