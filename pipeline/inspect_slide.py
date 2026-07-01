"""Dump the raw chunk text for the March YTD Net Sales slide so we can see
whether the per-brand vs PY% column survived extraction.

Usage (from repo root, venv active):
    python pipeline/inspect_slide.py
    python pipeline/inspect_slide.py Kisqali Cosentyx      # custom match terms
    python pipeline/inspect_slide.py --index product_strategy Kesimpta
"""
import json
import sys
from pathlib import Path

args = sys.argv[1:]
index = "financial_results"
if args and args[0] == "--index":
    index = args[1]
    args = args[2:]
terms = args or ["925", "Cosentyx"]

path = Path("chunks") / f"{index}.jsonl"
if not path.exists():
    print(f"NOT FOUND: {path} (run from repo root)")
    sys.exit(1)

hits = 0
for line in path.open(encoding="utf-8"):
    rec = json.loads(line)
    text = rec.get("text", "")
    if all(t in text for t in terms):
        hits += 1
        print("=" * 70)
        print(f"doc_id={rec.get('doc_id')}  page={rec.get('page')}  "
              f"fiscal_period={rec.get('fiscal_period')}  "
              f"period_scope={rec.get('period_scope')}")
        print("-" * 70)
        print(text[:4000])
        print()
        if hits >= 3:
            break

if hits == 0:
    print(f"No chunk in {path} contains all of: {terms}")
    print("Try different terms, e.g.:  python pipeline/inspect_slide.py Kisqali")
