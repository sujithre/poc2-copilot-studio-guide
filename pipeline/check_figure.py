"""Trace a figure back to the page, heading and brand it came from.

For checking an answer by hand. Reads the local vision extracts only - no Azure,
no agent, no credentials.

    python pipeline/check_figure.py 43%
    python pipeline/check_figure.py 15.0% --brand Kesimpta
    python pipeline/check_figure.py 53% 13% 5% --brand Kesimpta

For each place the value appears it reports the document, page, the HEADING it
sits under, and - for KPI records - the brand the extractor attributed it to.

Two things go wrong in an answer, and both show up here:

  WRONG BRAND    the value belongs to a competitor's series. Pass --brand to
                 have those flagged: the checker compares the brand you were
                 told against the brand the record names.

  WRONG METRIC   the value is real but sits under a different heading than the
                 metric being discussed - net sales growth quoted as a
                 prescription trend, say. The heading is printed for every hit
                 so the substitution is visible without opening the deck.

Values are matched as written, so "43%" does not match "4.3%" or "143%".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import paths  # noqa: E402


def _page_no(path: str) -> int:
    digits = "".join(c for c in os.path.basename(path) if c.isdigit())
    return int(digits) if digits else 0


def _headings_and_lines(markdown: str) -> list[tuple[str, str]]:
    """(heading, line) for every non-heading line, carrying the nearest heading."""
    out: list[tuple[str, str]] = []
    heading = ""
    for raw in (markdown or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = line.lstrip("# ").strip()
            continue
        out.append((heading, line))
    return out


def _contains(haystack: str, value: str) -> bool:
    """Whole-token match, so 43% does not match 143% or 4.3%."""
    return re.search(rf"(?<![\d.,]){re.escape(value)}(?![\d.,%])", haystack) is not None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("values", nargs="+", help="figures exactly as the answer printed them")
    ap.add_argument("--brand", default="",
                    help="the brand the answer attributed them to; mismatches are flagged")
    ap.add_argument("--pages", default=str(paths()["vision"]))
    ap.add_argument("--context", type=int, default=110, help="characters of line to show")
    args = ap.parse_args()

    files = sorted(glob(os.path.join(args.pages, "**", "page*.json"), recursive=True),
                   key=_page_no)
    if not files:
        print(f"no page*.json under {args.pages}")
        return 0
    print(f"searching {len(files)} pages under {args.pages}\n")

    claimed = args.brand.strip().lower()
    for value in args.values:
        print("=" * 78)
        print(f"VALUE: {value}" + (f"   (answer says: {args.brand})" if args.brand else ""))
        print("=" * 78)
        hits = 0
        mismatches = 0

        for path in files:
            with open(path, encoding="utf-8") as fh:
                page = json.load(fh)
            doc = os.path.basename(os.path.dirname(path))
            pg = os.path.basename(path)

            # --- KPI records: these carry an explicit brand -------------------
            for kp in page.get("kpis") or []:
                for field in ("value", "delta_value"):
                    if not _contains(str(kp.get(field) or ""), value):
                        continue
                    hits += 1
                    owner = str(kp.get("brand") or "").strip()
                    flag = ""
                    if claimed:
                        if not owner:
                            flag = "   <-- record names NO brand"
                        elif owner.lower() != claimed:
                            flag = f"   <-- BELONGS TO {owner.upper()}, NOT {args.brand.upper()}"
                            mismatches += 1
                    print(f"  KPI  {doc}/{pg}")
                    print(f"       metric : {kp.get('name','')!r}")
                    print(f"       scope  : {kp.get('scope','')!r}   period: {kp.get('period','')!r}"
                          f"   category: {kp.get('category','')!r}")
                    print(f"       brand  : {owner or '(none)'}{flag}")
                    print()
                    break

            # --- markdown lines: these carry a heading ------------------------
            for heading, line in _headings_and_lines(page.get("markdown") or ""):
                if not _contains(line, value):
                    continue
                hits += 1
                print(f"  TEXT {doc}/{pg}")
                print(f"       heading: {heading or '(none)'}")
                print(f"       line   : {line[:args.context]}")
                print()

        if not hits:
            print("  NOT FOUND anywhere in the corpus - the answer did not get this"
                  " from the source as written.\n")
        elif mismatches:
            print(f"  {mismatches} of {hits} occurrence(s) belong to a DIFFERENT brand"
                  f" than {args.brand}.\n")
        else:
            print(f"  {hits} occurrence(s).\n")

    print("Check each hit's HEADING against the metric the answer claimed, and each")
    print("KPI's BRAND against the brand the answer named. Those are the two ways a")
    print("real number ends up in a false sentence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
