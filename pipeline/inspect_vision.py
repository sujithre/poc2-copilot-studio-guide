"""Quick visual summary of vision extraction output for one doc.

Usage:
  python inspect_vision.py <doc_id>
  python inspect_vision.py us-results-2026-03
"""
from __future__ import annotations
import json
import sys

from common import paths


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python inspect_vision.py <doc_id>", file=sys.stderr)
        return 2
    doc_id = sys.argv[1]
    p = paths()
    vision_dir = p["vision"] / doc_id
    files = sorted(vision_dir.glob("page*.json"))
    print(f"Pages: {len(files)}\n")
    totals = dict(tables=0, figures=0, kpis=0, brands=0, compounds=0)
    for fp in files:
        r = json.loads(fp.read_text(encoding="utf-8"))
        print(
            f"p{r.get('page', 0):>3} | kind={r.get('page_kind',''):<20} | fp={r.get('fiscal_period',''):<14} "
            f"| tables={len(r.get('tables', []))} figs={len(r.get('figures', []))} "
            f"kpis={len(r.get('kpis', []))} brands={len(r.get('brands', []))} | "
            f"fwd={r.get('is_forward_looking')} | status={r.get('_status','?')}"
        )
        totals['tables']    += len(r.get('tables', []))
        totals['figures']   += len(r.get('figures', []))
        totals['kpis']      += len(r.get('kpis', []))
        totals['brands']    += len(r.get('brands', []))
        totals['compounds'] += len(r.get('compound_codes', []))
    print("\nTotals:", totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
