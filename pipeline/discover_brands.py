"""Brand discovery: scan chunks/*.jsonl for unregistered brand candidates.

Pipeline position: run AFTER chunker.py, BEFORE index_upload.py.

Inputs:
  - chunks/*.jsonl       (one per logical index)
  - manifest.brand_registry  (used as the "already known" filter)

Outputs:
  - kpi/brand_candidates.json  (human review file; NOT uploaded to the index)

How candidates are collected:
  1. Every value in chunk.brand_mentions that is NOT a registered canonical name.
  2. Every section-heading-like token mined from chunk text (`## Heading` lines)
     that is NOT a registered alias and passes the `_looks_brand_like` filter.

Each candidate gets:
  - mentions   : how many chunks reference it
  - docs       : list of doc_ids it appears in
  - sample_contexts : up to 3 short text snippets surrounding its first mentions
  - llm_verdict (optional, --llm flag) : yes/no/unsure judgement from the
                                          configured Azure OpenAI deployment
  - suggested_ta (optional, --llm flag): "oncology" | "immunology" | ... or null

Usage:
  python discover_brands.py
  python discover_brands.py --llm                # adds LLM validation
  python discover_brands.py --min-mentions 2     # threshold (default 1)
  python discover_brands.py --only external_messages
"""
from __future__ import annotations
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from common import env, load_manifest, paths, BrandRegistry
from chunker import _looks_brand_like

# Reuse stopwords and add a few more that show up in chunked text
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_EXTRA_STOPWORDS = {
    "Net Sales", "Cost", "Revenue", "OPEX", "Gross Margin",
    "Pipeline", "Portfolio", "Innovation", "Performance",
    "Cardio-Renal Metabolic", "Immunology", "Oncology", "Neuroscience",
    "Updated Notes", "Strategy", "Ambition",
}


def collect_from_jsonl(jsonl_path: Path) -> list[dict]:
    out: list[dict] = []
    if not jsonl_path.exists():
        return out
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def mine_heading_candidates(text: str) -> list[str]:
    """Pull `## Heading` lines from chunk text."""
    out: list[str] = []
    for m in _HEADING_RE.finditer(text or ""):
        h = m.group(1).strip().rstrip(":").strip()
        if h and _looks_brand_like(h) and h not in _EXTRA_STOPWORDS:
            out.append(h)
    return out


def first_context(text: str, token: str, width: int = 80) -> str:
    if not text or not token:
        return ""
    idx = text.lower().find(token.lower())
    if idx == -1:
        return text[:width].strip()
    start = max(0, idx - width // 2)
    end = min(len(text), idx + len(token) + width // 2)
    snippet = text[start:end].replace("\n", " ").strip()
    return ("..." if start > 0 else "") + snippet + ("..." if end < len(text) else "")


def discover(chunks_dir: Path, registry: BrandRegistry, only: list[str] | None,
             min_mentions: int) -> list[dict]:
    candidates: dict[str, dict] = defaultdict(lambda: {
        "mentions": 0,
        "docs": set(),
        "sample_contexts": [],
        "sources": set(),  # 'mentions_field' | 'heading'
    })

    for jsonl in sorted(chunks_dir.glob("*.jsonl")):
        idx_name = jsonl.stem
        if only and idx_name not in only:
            continue
        for rec in collect_from_jsonl(jsonl):
            doc_id = rec.get("doc_id", "")
            text = rec.get("text", "")

            # Source 1: brand_mentions field that isn't already canonical
            for m in (rec.get("brand_mentions") or []):
                if registry.is_known(m):
                    continue
                cand = candidates[m]
                cand["mentions"] += 1
                cand["docs"].add(doc_id)
                cand["sources"].add("mentions_field")
                if len(cand["sample_contexts"]) < 3:
                    cand["sample_contexts"].append(first_context(text, m))

            # Source 2: heading-like phrases in chunk text
            for h in mine_heading_candidates(text):
                if registry.is_known(h):
                    continue
                cand = candidates[h]
                cand["mentions"] += 1
                cand["docs"].add(doc_id)
                cand["sources"].add("heading")
                if len(cand["sample_contexts"]) < 3:
                    cand["sample_contexts"].append(first_context(text, h))

    # Materialize and threshold
    out: list[dict] = []
    for token, info in candidates.items():
        if info["mentions"] < min_mentions:
            continue
        out.append({
            "token": token,
            "mentions": info["mentions"],
            "appears_in_docs": sorted(info["docs"]),
            "sources": sorted(info["sources"]),
            "sample_contexts": info["sample_contexts"],
        })
    out.sort(key=lambda c: (-c["mentions"], c["token"].lower()))
    return out


# ---------------------------------------------------------------------------
# Optional LLM validation
# ---------------------------------------------------------------------------

_LLM_SYSTEM = (
    "You classify candidate strings extracted from pharmaceutical/financial "
    "documents (Novartis US). For each candidate, decide whether it is most "
    "likely a pharmaceutical brand or compound name. "
    "Return JSON only: {\"verdict\":\"yes|no|unsure\",\"therapeutic_area\":"
    "\"oncology|immunology|cardio_renal_metabolic|neuroscience|other|null\","
    "\"reasoning\":\"<one short sentence>\"}."
)


def _llm_validate(client, deployment: str, token: str, contexts: list[str]) -> dict:
    user = (
        f"Candidate token: {token}\n\n"
        f"Sample contexts where it appears:\n" + "\n".join(f"- {c}" for c in contexts)
    )
    # Reasoning models (gpt-5, o-series) require `max_completion_tokens` and
    # do not accept `temperature`. Older models (gpt-4o, gpt-4.1) use
    # `max_tokens` and accept `temperature=0`. Detect from deployment name.
    is_reasoning = any(
        deployment.lower().startswith(p) for p in ("gpt-5", "o1", "o3", "o4")
    ) or "gpt-5" in deployment.lower()
    kwargs: dict = {
        "model": deployment,
        "timeout": 30,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": user},
        ],
    }
    if is_reasoning:
        # Reasoning models burn tokens internally; bump cap so the visible
        # JSON answer isn't truncated to nothing.
        kwargs["max_completion_tokens"] = 2000
    else:
        kwargs["max_tokens"] = 200
        kwargs["temperature"] = 0
    resp = client.chat.completions.create(**kwargs)
    raw = resp.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except Exception:
        return {"verdict": "unsure", "therapeutic_area": None, "reasoning": "parse error"}


def attach_llm_verdicts(candidates: list[dict]) -> None:
    from openai import AzureOpenAI
    from auth import aoai_token_provider
    client = AzureOpenAI(
        azure_endpoint=env("AZURE_OPENAI_ENDPOINT", required=True),
        azure_ad_token_provider=aoai_token_provider(),
        api_version=env("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )
    deployment = env("AZURE_OPENAI_VISION_DEPLOYMENT", required=True)
    for cand in candidates:
        verdict = _llm_validate(client, deployment, cand["token"], cand["sample_contexts"])
        cand["llm_verdict"] = verdict.get("verdict")
        cand["suggested_ta"] = verdict.get("therapeutic_area")
        cand["llm_reasoning"] = verdict.get("reasoning")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=None,
                    help="Limit to specific logical index names (repeatable)")
    ap.add_argument("--min-mentions", type=int, default=1,
                    help="Drop candidates with fewer mentions than this (default 1)")
    ap.add_argument("--llm", action="store_true",
                    help="Use Azure OpenAI to classify each candidate (yes/no/unsure)")
    ap.add_argument("--out", default=None,
                    help="Output path (default kpi/brand_candidates.json)")
    args = ap.parse_args()

    p = paths()
    manifest = load_manifest()
    registry = BrandRegistry(manifest)

    candidates = discover(p["chunks"], registry, args.only, args.min_mentions)

    if args.llm and candidates:
        print(f"Running LLM validation on {len(candidates)} candidates...")
        attach_llm_verdicts(candidates)

    out_path = Path(args.out) if args.out else (p["kpi"] / "brand_candidates.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_for_indices": args.only or list(manifest["indices"]),
        "min_mentions": args.min_mentions,
        "registered_brands_skipped": registry.all_canonical,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nWrote {len(candidates)} candidates -> {out_path}")
    if candidates:
        print("\nTop candidates:")
        for c in candidates[:15]:
            verdict = f"  llm={c.get('llm_verdict','-')}" if args.llm else ""
            print(f"  - {c['token']:<24} mentions={c['mentions']:<3} docs={len(c['appears_in_docs'])}{verdict}")
        print("\nReview the JSON, then add real brands to manifest.brand_registry.brands")
        print("and re-run chunker.py + index_upload.py to canonicalize them.")


if __name__ == "__main__":
    main()
