"""Blast-radius check for narrative segment classification.

Run this in the environment that holds the real page extracts, BEFORE re-chunking.

    python pipeline/verify_segments.py --pages <dir-with-page*.json>

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

from chunker import SegmentRegistry, narrative_segment  # noqa: E402


def _page_no(path: str) -> int:
    digits = "".join(c for c in os.path.basename(path) if c.isdigit())
    return int(digits) if digits else 0


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default=os.path.join(root, "pages"),
                    help="directory containing page*.json extracts (searched recursively)")
    ap.add_argument("--manifest", default=os.path.join(root, "manifest.json"))
    ap.add_argument("--show", type=int, default=0,
                    help="also print the measured lines for the first N demoted pages")
    args = ap.parse_args()

    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    segments = SegmentRegistry(manifest)
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
    paths = sorted(glob(os.path.join(args.pages, "**", "page*.json"), recursive=True),
                   key=_page_no)
    if not paths:
        print(f"\nno page*.json found under {args.pages}")
        return 0

    demoted: list[tuple[str, str, str]] = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            page = json.load(fh)
        level, name = narrative_segment(segments, page.get("markdown") or "")
        if level == "subsegment":
            demoted.append((path, name, page.get("markdown") or ""))

    print("\nNARRATIVE CLASSIFICATION")
    print(f"  pages         : {len(paths)}")
    print(f"  stay total    : {len(paths) - len(demoted)}")
    print(f"  demoted       : {len(demoted)}  ({100 * len(demoted) / len(paths):.0f}%)")
    if len(demoted) > len(paths) * 0.35:
        print("  ^^ REVIEW: a demotion rate this high usually means an alias is too broad")

    print("\nDEMOTED PAGES  (evidence_boost 3 -> 1)")
    for path, name, _ in demoted:
        rel = os.path.relpath(path, args.pages)
        print(f"  {rel:<44} -> {name}")

    from chunker import _MEASURE_RE  # noqa: WPS436  (report-only introspection)
    for path, name, md in demoted[:args.show]:
        print(f"\n  --- measured lines in {os.path.relpath(path, args.pages)} ---")
        for raw in md.splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and _MEASURE_RE.search(line):
                print(f"    [{segments.detect(line) or 'TOTAL'}] {line[:110]}")

    print("\nALIAS SELF-CHECK:", "PASS" if ok else "FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
