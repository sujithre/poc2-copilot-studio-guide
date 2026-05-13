"""Shared helpers for POC2: load .env, manifest, paths, brand registry.

Layout:
  POC2/
    manifest.json
    docs/                <- source PDFs (converted from .pptx / .docx)
    PDF_pages/<doc_id>/  <- rasterized PNGs per doc
    PDF_vision/<doc_id>/ <- per-page vision JSON per doc
    chunks/              <- one .jsonl per logical index
    kpi/                 <- per-doc KPI sidecars
    pipeline/            <- this folder

Env loading order:
  1. POC2/.env  (if present, POC2-specific overrides)
  2. ../.env    (workspace root .env, shared with the original pipeline)
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]            # POC2/
WORKSPACE_ROOT = ROOT.parent                          # C:\Projects\Bio
load_dotenv(WORKSPACE_ROOT / ".env")                  # base
load_dotenv(ROOT / ".env", override=True)             # POC2 overrides win


def env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val  # type: ignore[return-value]


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).resolve()


def paths() -> dict[str, Path]:
    return {
        "root":   expand_path(env("POC2_ROOT",   str(ROOT))),
        "docs":   expand_path(env("POC2_DOCS",   str(ROOT / "docs"))),
        "pages":  expand_path(env("POC2_PAGES",  str(ROOT / "PDF_pages"))),
        "vision": expand_path(env("POC2_VISION", str(ROOT / "PDF_vision"))),
        "chunks": expand_path(env("POC2_CHUNKS", str(ROOT / "chunks"))),
        "kpi":    expand_path(env("POC2_KPI",    str(ROOT / "kpi"))),
    }


def load_manifest() -> dict:
    with open(ROOT / "manifest.json", "r", encoding="utf-8") as f:
        return json.load(f)


def docs_to_process(manifest: dict, only_pilot: bool | None = None) -> list[dict]:
    """Return the documents we should process based on manifest.scope."""
    if only_pilot is None:
        only_pilot = not manifest.get("scope", {}).get("run_all", False)
    pilot_id = manifest.get("scope", {}).get("pilot_doc_id")
    docs = manifest["documents"]
    if only_pilot and pilot_id:
        return [d for d in docs if d["doc_id"] == pilot_id]
    return docs


# ---------------------------------------------------------------------------
# Brand registry helper
# ---------------------------------------------------------------------------

class BrandRegistry:
    """Canonical brand name lookup with auto-extension from documents[*].brand.

    - Loads from manifest.brand_registry.brands.
    - Auto-adds any brand referenced in documents[*].brand that is missing.
    - Lookups are case-insensitive on aliases.
    """

    def __init__(self, manifest: dict) -> None:
        registry = (manifest.get("brand_registry") or {}).get("brands", []) or []
        self._canonical_to_ta: dict[str, str | None] = {}
        self._alias_to_canonical: dict[str, str] = {}

        for entry in registry:
            canonical = entry["canonical"]
            ta = entry.get("therapeutic_area")
            self._canonical_to_ta[canonical] = ta
            for alias in entry.get("aliases") or [canonical]:
                self._alias_to_canonical[alias.lower()] = canonical

        # Auto-extend with any brand named directly on a document
        for doc in manifest.get("documents", []):
            brand = doc.get("brand")
            if not brand:
                continue
            if brand not in self._canonical_to_ta:
                self._canonical_to_ta[brand] = None
                self._alias_to_canonical.setdefault(brand.lower(), brand)

    def canonicalize(self, name: str) -> str | None:
        if not name:
            return None
        return self._alias_to_canonical.get(name.strip().lower())

    def therapeutic_area(self, canonical: str) -> str | None:
        return self._canonical_to_ta.get(canonical)

    def normalize_list(self, names: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for n in names or []:
            c = self.canonicalize(n) or n
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def split_known_unknown(self, names: list[str]) -> tuple[list[str], list[str]]:
        """Return (canonical_known, raw_unknown).
        - canonical_known: registry-canonical names for inputs that resolve.
        - raw_unknown: cleaned raw names for inputs that don't resolve."""
        known: list[str] = []
        unknown: list[str] = []
        seen_k: set[str] = set()
        seen_u: set[str] = set()
        for n in names or []:
            if not n:
                continue
            c = self.canonicalize(n)
            if c:
                if c not in seen_k:
                    seen_k.add(c)
                    known.append(c)
            else:
                cleaned = n.strip()
                if cleaned and cleaned not in seen_u:
                    seen_u.add(cleaned)
                    unknown.append(cleaned)
        return known, unknown

    def is_known(self, name: str) -> bool:
        return self.canonicalize(name) is not None

    @property
    def all_canonical(self) -> list[str]:
        return sorted(self._canonical_to_ta.keys())


# ---------------------------------------------------------------------------
# IR notes section detector (Part 0..Part 4 with TA mapping)
# ---------------------------------------------------------------------------

import re as _re


class IRNotesSectionDetector:
    """Detects Part-level headings inside IR notes markdown.

    Reads `manifest.section_taxonomies.ir_notes`. Each part has a list of
    `match` phrases. A line is treated as a Part marker when:
      1. The line is heading-like:
         - starts with markdown `#`, OR
         - is short (<= 60 chars) AND not a bullet/quote/numbered item AND
           does not end with sentence terminal (.!?).
      2. After stripping markdown markers, one of the part's `match` phrases
         appears as a contiguous *word* subsequence of the line
         (case-insensitive). Longest match wins.

    The contiguous-subsequence + heading filter together ensure that:
      - "Part 2 Immunology" -> part_2_imm (Immunology, len 10) beats
        part_2_crm (Part 2, len 6).
      - "- Oncology pipeline updates" -> no match (bullet item).
      - "Random sentence about oncology drugs." -> no match (terminal `.`).
    """

    MAX_HEADING_LEN = 60
    SENTENCE_TERMINALS = ".!?"
    _LIST_PREFIX = _re.compile(r"^([-*\u2022]\s|>\s|\d+[.)]\s)")

    def __init__(self, manifest: dict) -> None:
        tax = (manifest.get("section_taxonomies") or {}).get("ir_notes") or {}
        self.parts: list[dict] = tax.get("parts") or []
        self.preamble: list[str] = tax.get("preamble_sections") or []
        # Pre-tokenize each match phrase into lowercase word tokens
        self._part_phrase_tokens: list[tuple[dict, list[list[str]]]] = []
        for part in self.parts:
            tokenised = []
            for phrase in (part.get("match") or []):
                toks = _re.findall(r"[A-Za-z0-9]+", phrase.lower())
                if toks:
                    tokenised.append(toks)
            self._part_phrase_tokens.append((part, tokenised))

    @staticmethod
    def _strip_markers(raw: str) -> str:
        # Strip leading markdown markers but only those at the very start
        return _re.sub(r"^[#>\-*\s]+", "", raw).strip()

    def _looks_heading(self, raw_stripped: str, cleaned: str) -> bool:
        if not cleaned:
            return False
        if raw_stripped.startswith("#"):
            return True
        if self._LIST_PREFIX.match(raw_stripped):
            return False
        if len(cleaned) > self.MAX_HEADING_LEN:
            return False
        if cleaned[-1] in self.SENTENCE_TERMINALS:
            return False
        return True

    @staticmethod
    def _contains_subseq(haystack: list[str], needle: list[str]) -> bool:
        if not needle or len(needle) > len(haystack):
            return False
        n = len(needle)
        for i in range(len(haystack) - n + 1):
            if haystack[i:i + n] == needle:
                return True
        return False

    def detect_part(self, line: str) -> dict | None:
        raw = (line or "").strip()
        if not raw:
            return None
        cleaned = self._strip_markers(raw)
        if not self._looks_heading(raw, cleaned):
            return None
        line_tokens = _re.findall(r"[A-Za-z0-9]+", cleaned.lower())
        if not line_tokens:
            return None

        best_score = 0
        best_part: dict | None = None
        for part, phrase_token_lists in self._part_phrase_tokens:
            for needle in phrase_token_lists:
                if self._contains_subseq(line_tokens, needle):
                    score = sum(len(t) for t in needle)  # weight by char length
                    if score > best_score:
                        best_score = score
                        best_part = part
        return best_part

    def part_number(self, part: dict) -> int:
        m = _re.search(r"\d+", part.get("id", ""))
        return int(m.group(0)) if m else 0


# ---------------------------------------------------------------------------
# Text-style classifier (prose / bullet_list / quote)
# ---------------------------------------------------------------------------

_BULLET_RE = _re.compile(r"^\s*([-*•]|\d+[.)])\s+")
_QUOTE_RE = _re.compile(r"^\s*>\s+")


def classify_text_style(text: str) -> str:
    """Heuristically pick a chunk_type based on the dominant line style."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "prose"
    n = len(lines)
    bullets = sum(1 for ln in lines if _BULLET_RE.match(ln))
    quotes = sum(1 for ln in lines if _QUOTE_RE.match(ln))
    if bullets / n >= 0.6:
        return "bullet_list"
    if quotes / n >= 0.5:
        return "quote"
    return "prose"


# ---------------------------------------------------------------------------
# Markdown section splitter (by `## ...` headings)
# ---------------------------------------------------------------------------

_HEADING_RE = _re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")


def split_markdown_sections(md: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) sections.

    The first section's heading is "" if the doc starts with body text before
    any heading. Headings of any level (#..######) act as boundaries.
    """
    if not md:
        return []
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in md.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            sections.append((m.group(2).strip(), []))
        else:
            sections[-1][1].append(line)
    return [(h, "\n".join(body).strip()) for h, body in sections if (h or "\n".join(body).strip())]
