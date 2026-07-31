"""Blast-radius check for narrative segment classification.

Run this BEFORE re-chunking. It reads the SAME vision extracts the chunker reads
and writes nothing, so it is safe to run at any time.

    python pipeline/verify_segments.py
    python pipeline/verify_segments.py --show 3

It answers one question: which page narratives does the new rule demote from
`total` to `subsegment`? A demoted page loses evidence_boost 3 -> 1, so it no
longer competes with the headline slide. That is correct for a page whose every
stated figure is a sub-cut, and wrong for anything else - so read the list.

Exit code is 0 always; this reports, it does not gate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chunker import PRODUCT_STRATEGY_INDEX, SegmentRegistry, narrative_segment  # noqa: E402
from common import load_manifest, paths  # noqa: E402


def _page_no(path: str) -> int:
    digits = "".join(c for c in os.path.basename(path) if c.isdigit())
    return int(digits) if digits else 0


def _routing(manifest: dict) -> dict[str, tuple[str, str]]:
    """doc_id -> (primary_index, doc_type). The vision sub-directory IS the doc_id."""
    return {d.get("doc_id", ""): (d.get("primary_index", ""), d.get("doc_type", ""))
            for d in manifest.get("documents", [])}


def _applies(index: str, doc_type: str) -> bool:
    """Does the chunker actually apply this rule to the page?

    Only product-strategy docs are classified, and IR notes take a separate
    chunking path entirely. Everything else is reported for information only.
    """
    return index == PRODUCT_STRATEGY_INDEX and doc_type != "ir_notes"


def main() -> int:
    p = paths()
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default=str(p["vision"]),
                    help="vision-extract root holding page*.json (searched recursively); "
                         "defaults to the same directory the chunker reads")
    ap.add_argument("--show", type=int, default=0,
                    help="also print the measured lines for the first N demoted pages")
    args = ap.parse_args()

    segments = SegmentRegistry(load_manifest())
    routing = _routing(load_manifest())
    print(f"pages root      : {args.pages}")
    print(f"registry        : {len(segments.all_canonical)} segments")

    # --- alias matching, incl. plural tolerance -----------------------------
    cases = [
        ("Kesimpta gained share in LET switches YTD March(+3.8pts vs PY)", "switch"),
        ("Kesimpta gained share in 1L YTD March", "1L"),
        ("Kesimpta share with generalists declined (-0.7pts MS market)", "generalists"),
        ("B-cell market declined (-3%)", "B-cell"),
        ("YTD NBRxs +1% vs TGT, TRx +1% vs TGT, demand units +4% vs TGT.", ""),
        ("Apr YTD sales finished at $1.027B, +23% vs PY, +7% vs TGT", ""),
    ]
    print("\nALIAS MATCHING")
    ok = True
    for text, want in cases:
        got = segments.detect(text)
        good = got == want
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  want={want!r:<14} got={got!r:<14} {text[:50]}")

    # --- classify every page ------------------------------------------------
    page_files = sorted(glob(os.path.join(args.pages, "**", "page*.json"), recursive=True),
                        key=_page_no)
    if not page_files:
        print(f"\nno page*.json found under {args.pages}")
        return 0

    scored = 0
    unknown: set[str] = set()
    demoted: list[tuple[str, str, str]] = []
    ignored: list[tuple[str, str, str]] = []
    for path in page_files:
        doc_id = os.path.basename(os.path.dirname(path))
        if doc_id not in routing:
            unknown.add(doc_id)
        index, doc_type = routing.get(doc_id, ("", ""))
        in_scope = _applies(index, doc_type)
        if in_scope:
            scored += 1
        with open(path, encoding="utf-8") as fh:
            page = json.load(fh)
        level, name = narrative_segment(segments, page.get("markdown") or "")
        if level == "subsegment":
            bucket = demoted if in_scope else ignored
            bucket.append((path, name, page.get("markdown") or ""))

    rate = f"{100 * len(demoted) / scored:.0f}% of in-scope" if scored else "no in-scope pages"
    print("\nNARRATIVE CLASSIFICATION")
    print(f"  pages         : {len(page_files)}")
    print(f"  in scope      : {scored}   (product_strategy, non-IR - the rule runs only here)")
    print(f"  demoted       : {len(demoted)}  ({rate})")
    if scored and len(demoted) > scored * 0.35:
        print("  ^^ REVIEW: a demotion rate this high usually means an alias is too broad")

    if unknown:
        print("\nUNKNOWN DOCUMENTS  (vision folder has no manifest entry - the chunker")
        print("                    will not process these, and this report cannot route them)")
        for doc_id in sorted(unknown):
            print(f"  {doc_id}")

    print("\nDEMOTED PAGES  (evidence_boost 3 -> 1)")
    for path, name, _ in demoted:
        rel = os.path.relpath(path, args.pages)
        print(f"  {rel:<44} -> {name}")
    if not demoted:
        print("  none")

    if ignored:
        print("\nNOT AFFECTED  (classified, but outside the rule's scope)")
        for path, name, _ in ignored:
            rel = os.path.relpath(path, args.pages)
            doc_id = os.path.basename(os.path.dirname(path))
            index, doc_type = routing.get(doc_id, ("", ""))
            print(f"  {rel:<44} -> {name:<12} [{index or '?'}/{doc_type or '?'}]")

    from chunker import _is_narrative_measure_line  # noqa: WPS436  (report-only)
    for path, name, md in demoted[:args.show]:
        print(f"\n  --- measured lines in {os.path.relpath(path, args.pages)} ---")
        for raw in md.splitlines():
            line = raw.strip()
            if _is_narrative_measure_line(line):
                print(f"    [{segments.detect(line) or 'TOTAL'}] {line[:110]}")

    print("\nALIAS SELF-CHECK:", "PASS" if ok else "FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
