"""Convert per-page vision JSON into chunks.jsonl per index, plus KPI sidecars.

Routing rules (matches manifest.indices):
  - page_kind in (cover/disclaimer/agenda/toc/references)  -> idx_meta
  - everything else                                         -> doc.primary_index

Doc -> primary_index mapping comes from manifest.documents[*].primary_index:
  - monthly_results        -> financial_results
  - ir_notes               -> external_messages   (special path: section-aware)
  - quarterly_update       -> external_messages
  - brand_mbr              -> product_strategy
  - brand_strategy         -> product_strategy
  - launch_readiness       -> product_strategy

Chunk types emitted (subset of manifest.chunk_types):
  prose | bullet_list | quote | slide | table | table_row | kpi_row |
  chart | figure | infographic | image | meta
"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable

from common import (
    env,
    load_manifest,
    docs_to_process,
    paths,
    BrandRegistry,
    IRNotesSectionDetector,
    classify_text_style,
    split_markdown_sections,
)

EXTRACTOR_VERSION = "0.2.0"

# Map FigureKind from vision schema -> chunk_type bucket
CHART_KINDS = {
    "bar_chart", "stacked_bar_chart", "line_chart", "area_chart",
    "pie_chart", "donut_chart",
    "scatter_chart", "bubble_chart", "heatmap", "waterfall",
}
IMAGE_KINDS = {"image", "logo", "headshot", "map"}
INFOGRAPHIC_KINDS = {"infographic"}
# Anything else (diagram, other) -> "figure"


META_PAGE_KINDS = {"cover", "disclaimer", "agenda", "toc", "references"}


# Common false positives we never want to capture as brand mentions
_BRAND_STOPWORDS = {
    "policy", "guidance", "ambition", "introduction", "summary", "overview",
    "highlights", "agenda", "background", "context", "appendix", "references",
    "fda", "ema", "us", "usa", "europe", "global", "ex-us", "ww",
    "growth", "trend", "outlook", "update", "table", "figure", "chart",
    "innovation", "performance", "ambition", "strategy", "execution",
    "focus", "key", "core", "pipeline", "portfolio", "launch", "approval",
    "data", "results", "next", "steps", "Q1", "Q2", "Q3", "Q4", "FY",
}


def _looks_brand_like(heading: str) -> bool:
    """Heuristic: a section heading that probably names a drug/brand.

    Rules (ALL must hold):
      - 1 to 3 word tokens
      - Total length 4..30 chars (drug names typically fit this)
      - At least one alpha char
      - Not in stopword set (case-insensitive)
      - First letter is uppercase OR the entire heading is uppercase (PORMACT)
    """
    if not heading:
        return False
    h = heading.strip().rstrip(":").strip()
    if not (4 <= len(h) <= 30):
        return False
    tokens = h.split()
    if not (1 <= len(tokens) <= 3):
        return False
    if not any(c.isalpha() for c in h):
        return False
    if h.lower() in _BRAND_STOPWORDS:
        return False
    if any(t.lower() in _BRAND_STOPWORDS for t in tokens):
        return False
    # Must start with uppercase letter OR be ALLCAPS (drug acronym)
    return h[0].isupper() or h.isupper()


def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", s.strip().lower())
    return s.strip("-") or "untitled"


def stable_id(*parts: str) -> str:
    raw = "::".join(p for p in parts if p)
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def figure_chunk_type(fig_kind: str) -> str:
    if fig_kind in CHART_KINDS:
        return "chart"
    if fig_kind in IMAGE_KINDS:
        return "image"
    if fig_kind in INFOGRAPHIC_KINDS:
        return "infographic"
    return "figure"


# Last day of each month (non-leap; February handled separately).
_MONTH_END = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
              7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
_QUARTER_END_MONTH = {"1": 3, "2": 6, "3": 9, "4": 12}


def _last_day(year: int, month: int) -> int:
    if month == 2 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        return 29
    return _MONTH_END.get(month, 30)


def derive_period_end_date(fiscal_period: str, doc: dict) -> str:
    """Best-effort calendar end date (YYYY-MM-DD) for a fiscal_period string.

    Handles: 'YYYY-MM' (monthly close), 'Qn_YYYY', 'FY_YYYY'. Falls back to the
    document publication_date (also coerced to a month end) or ''.
    """
    fp = (fiscal_period or "").strip()
    # Monthly close: 2026-03
    m = re.fullmatch(r"(\d{4})-(\d{2})", fp)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}-{_last_day(y, mo):02d}"
    # Quarter: Q1_2026
    m = re.fullmatch(r"[Qq]([1-4])_(\d{4})", fp)
    if m:
        mo = _QUARTER_END_MONTH[m.group(1)]
        y = int(m.group(2))
        return f"{y:04d}-{mo:02d}-{_last_day(y, mo):02d}"
    # Full year: FY_2026
    m = re.fullmatch(r"(?:FY|fy)_(\d{4})", fp)
    if m:
        y = int(m.group(1))
        return f"{y:04d}-12-31"
    # Fall back to publication_date (YYYY-MM or YYYY-MM-DD)
    pub = (doc.get("publication_date") or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", pub)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        d = int(m.group(3)) if m.group(3) else _last_day(y, mo)
        if 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def _comparison_to_basis(text: str) -> list[str]:
    """Map a free-text comparison string to comparison_basis tokens."""
    t = (text or "").lower()
    out: list[str] = []
    if any(k in t for k in ("prior year", "prior-year", "vs py", " py", "yoy", "y/y", "last year")):
        out.append("vs_py")
    if any(k in t for k in ("tgt", "target", "budget", "plan")):
        out.append("vs_tgt")
    if any(k in t for k in ("latest outlook", "vs lo", " lo", "outlook")):
        out.append("vs_lo")
    if "consensus" in t:
        out.append("vs_consensus")
    # de-dup, keep order
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


# Normalized fiscal-period tokens we are willing to copy onto a chunk's
# `fiscal_period` (a filterable field). Freeform labels such as "March YTD"
# must NOT land here - they belong in `period_label`.
_NORMALIZED_FP_RE = re.compile(r"^(Q[1-4]_\d{4}|H[12]_\d{4}|FY_\d{4}|\d{4}-\d{2}|\d{4}-W\d{2})$")

_MONTH_NUM = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# A cumulative ("YTD") period ending in a given month maps to a calendar period.
# March-YTD == Q1, June-YTD == H1, Sept-YTD == 9 months (Q3 cumulative),
# Dec-YTD == full year. We only synthesise quarter/half/FY tokens here.
_YTD_MONTH_TO_PERIOD = {3: ("Q1", "quarter"), 6: ("H1", "half"),
                        9: ("Q3", "quarter"), 12: ("FY", "full_year")}


def _looks_like_fiscal_period(p: str) -> bool:
    return bool(_NORMALIZED_FP_RE.match((p or "").strip()))


def _year_from(*candidates: str) -> str:
    for c in candidates:
        m = re.search(r"(20\d{2})", c or "")
        if m:
            return m.group(1)
    return ""


def normalize_ytd_label(label: str, year: str) -> tuple[str, str, str]:
    """Map a freeform cumulative label like "March YTD" / "Mar YTD" to a
    normalized (fiscal_period, scope, quarter_token).

    Returns ("", "", "") when the label is not a recognisable month-YTD form.
    e.g. ("March YTD", "2026") -> ("Q1_2026", "ytd", "Q1").
    """
    t = (label or "").lower()
    if "ytd" not in t and "year to date" not in t and "year-to-date" not in t:
        return "", "", ""
    month_no = None
    for name, num in _MONTH_NUM.items():
        if re.search(rf"\b{name}\b", t):
            month_no = num
            break
    if month_no is None:
        return "", "", ""
    period = _YTD_MONTH_TO_PERIOD.get(month_no)
    if not period or not year:
        return "", "", ""
    token, scope = period  # e.g. ("Q1", "quarter")
    fp = f"{token}_{year}"
    return fp, "ytd", token


def _add_quarter(out: list[str], q: str, yr: str) -> None:
    if yr:
        out.extend([f"Q{q} {yr}", f"Q{q}_{yr}", f"Q{q}'{yr[2:]}", f"{yr} Q{q}"])
    else:
        out.append(f"Q{q}")


def _month_fiscal_period(period: str, year: str) -> str:
    """Map a single-month label ("Mar"/"March") + year to a YYYY-MM token."""
    t = (period or "").lower()
    for name, num in _MONTH_NUM.items():
        if re.search(rf"\b{name}\b", t):
            if year:
                return f"{year}-{num:02d}"
            return ""
    return ""


def period_search_aliases(fiscal_period: str, period_label: str,
                          period_end_date: str = "") -> str:
    """Searchable period aliases so hybrid/BM25 retrieval matches quarter-style
    queries (e.g. "Q1 net sales") even when the printed label is "March YTD".
    A March-YTD page in a monthly deck *is* Q1, but that word never appears in
    the source text, so we synthesise it from the normalized period.

    NOTE: this is the *page/slide-level* helper. KPI rows use
    kpi_period_aliases() which is scope-aware so single-month and rolling
    figures never get mis-tagged as the quarter.
    """
    fp = (fiscal_period or "").strip()
    out: list[str] = []

    mq = re.match(r"^Q([1-4])_(\d{4})$", fp)
    if mq:
        _add_quarter(out, mq.group(1), mq.group(2))
    elif re.match(r"^FY_\d{4}$", fp):
        yr = fp.split("_")[1]
        out += [fp, f"FY {yr}", f"full year {yr}"]
    elif fp and fp.upper() != "UNKNOWN":
        out.append(fp)

    # Synthesise quarter/half/FY tokens from a "March YTD"-style label too, so a
    # chunk whose fiscal_period is still the freeform string is reachable by a
    # "Q1" query.
    yr = _year_from(fp, period_label, period_end_date)
    norm_fp, _scope, token = normalize_ytd_label(period_label or fp, yr)
    if token:
        mq2 = re.match(r"^Q([1-4])$", token)
        if mq2:
            _add_quarter(out, mq2.group(1), yr)
        else:
            out.append(f"{token} {yr}".strip())

    if period_label:
        out.append(period_label)
    # dedupe preserving order
    return " ".join(dict.fromkeys(out))


def kpi_period_aliases(kp: dict, fallback_fp: str = "", period_label: str = "",
                       period_end_date: str = "") -> str:
    """Scope-aware search aliases for a SINGLE KPI cell.

    Critically, the quarter token is derived from the KPI's OWN period/scope,
    not the page default. This prevents single-month figures (e.g. "Mar" 310)
    and rolling/annual figures (e.g. "1FP26" 4,144) from being mis-tagged as
    "Q1" just because they sit on a Q1 page. Only genuine quarter or
    cumulative-YTD-mapping-to-quarter rows get the Q-token.
    """
    period = (kp.get("period") or "").strip()
    scope = (kp.get("period_scope") or "").strip().lower()
    yr = _year_from(period, fallback_fp, period_label, period_end_date)
    out: list[str] = []

    mq = re.search(r"\bQ([1-4])\b", period)
    if mq:
        # KPI explicitly names its quarter.
        _add_quarter(out, mq.group(1), yr)
    elif scope == "ytd":
        # Cumulative YTD -> calendar period (March YTD == Q1).
        _, _, token = normalize_ytd_label(period, yr)
        tq = re.match(r"^Q([1-4])$", token or "")
        if tq:
            _add_quarter(out, tq.group(1), yr)
        elif token:
            out.append(f"{token} {yr}".strip())
    elif scope == "month":
        # Single month: month aliases only - NEVER a quarter token.
        if period:
            out.append(f"{period} {yr}".strip())
    elif _looks_like_fiscal_period(period):
        out.append(period)

    # Always include the human label for lexical matching.
    if period:
        out.append(period)
    return " ".join(dict.fromkeys(o for o in out if o))


# Business-language <-> slide-abbreviation synonyms. Each entry maps a set of
# trigger substrings (as they appear in a KPI's name/comparison/basis/unit) to
# the extra search terms to inject so a user typing the long form (or the short
# form) still retrieves the row. Keep this small and high-signal - it only
# needs to bridge the words users actually type to the words on the slide.
_TERM_SYNONYMS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("gtn", "gross-to-net", "gross to net"),
     ("GTN", "gross-to-net", "gross to net", "net pricing")),
    (("vs tgt", "vs. tgt", "target", "budget", "plan"),
     ("vs target", "vs TGT", "target", "budget", "actual vs target")),
    (("vs py", "vs. py", "prior year", "previous year", "yoy", "year-over-year"),
     ("vs prior year", "vs PY", "prior year", "previous year", "YoY")),
    (("price volume mix", "pvm", "vol/pri"),
     ("price volume mix", "PVM", "price volume mix bridge")),
    (("w/s sit", "wholesaler", "doh", "days on hand", "sit days"),
     ("wholesaler inventory", "W/S SIT", "days on hand", "DoH")),
)


def kpi_term_aliases(kp: dict) -> str:
    """Synonym aliases so business-language queries reach the right KPI row.

    A user asking "gross-to-net impact" or "actual net sales vs target" should
    retrieve the GTN / vs-TGT rows even though the slide only prints "GTN" /
    "vs TGT". We scan the KPI's descriptive fields and inject both forms.
    """
    haystack = " ".join(str(kp.get(k, "")) for k in (
        "name", "comparison", "comparison_basis", "basis", "measure_basis",
        "unit", "source_quote",
    )).lower()
    out: list[str] = []
    for triggers, terms in _TERM_SYNONYMS:
        if any(t in haystack for t in triggers):
            out.extend(terms)
    # measure_basis=actual -> let "actual"/"reported" queries match.
    mb = (kp.get("measure_basis") or "").lower()
    if mb == "actual":
        out.extend(("actual", "actuals", "reported"))
    elif mb in ("outlook_lo", "outlook"):
        out.extend(("Latest Outlook", "outlook", "forecast"))
    elif mb == "target":
        out.extend(("target", "budget", "plan"))
    return " ".join(dict.fromkeys(out))


def narrow_meta_brand(meta: dict, raw_brand) -> dict:
    """Specialise a chunk's filterable ``brand`` to the brand the row actually
    reports.

    Three cases on a page whose ``brand`` list has MORE THAN ONE brand:
    1. ``raw_brand`` matches exactly one page brand -> pin ``brand`` /
       ``brand_mentions`` to just that brand (e.g. the page-13 ``Brand=KISQALI``
       row).
    2. ``raw_brand`` is empty or does not resolve to a single page brand -> the
       row is an aggregate / total (US, Total Priority, Mature, Gx, "% of Net
       Sales", company-wide Net Sales) so it is NOT attributable to any one
       brand: clear the filterable ``brand`` so a brand filter excludes it.
       ``brand_mentions`` is left intact for recall.

    On a single-brand page (<=1 brand) nothing is cleared: a total there IS that
    brand's total.
    """
    page_brands = meta.get("brand") or []
    if isinstance(raw_brand, (list, tuple)):
        raw_brand = raw_brand[0] if len(raw_brand) == 1 else ""
    raw_brand = (raw_brand or "").strip()
    match = (next((b for b in page_brands if b.lower() == raw_brand.lower()), None)
             if raw_brand else None)
    if match:
        m = dict(meta)
        m["brand"] = [match]
        m["brand_mentions"] = [match]
        return m
    if len(page_brands) > 1:
        # Aggregate / unattributable row on a multi-brand page: do not let it
        # inherit individual brand names as its authoritative attribution.
        m = dict(meta)
        m["brand"] = []
        return m
    return meta


def _row_brand(cols: list, row: list) -> str:
    """Best-effort single brand for a table row: a cell under a Brand/Product/
    Name column, else the first cell. Resolution to an actual brand is left to
    ``narrow_meta_brand`` (which only narrows on an exact page-brand match)."""
    for c, v in zip(cols, row):
        if str(c).strip().lower() in ("brand", "product", "name"):
            return str(v or "").strip()
    return str(row[0] or "").strip() if row else ""


def kpi_meta_override(meta: dict, kp: dict) -> dict:
    """Return a copy of the page meta with period/basis fields specialised to a
    single KPI cell. This is what makes the LO grid usable: each emitted KPI
    chunk carries its OWN period_scope / measure_basis / comparison_basis even
    though they all share one page.
    """
    m = dict(meta)
    if kp.get("period"):
        kp_period = kp["period"].strip()
        kp_scope = (kp.get("period_scope") or "").strip().lower()
        if _looks_like_fiscal_period(kp_period):
            # A genuinely normalized period (e.g. "FY_2026" on the LO grid):
            # safe to specialise the filterable fiscal_period.
            m["fiscal_period"] = kp_period
        elif kp_scope == "month":
            # Single-month cell on a quarter page: this row belongs to the
            # month, NOT the quarter. Pin fiscal_period to YYYY-MM so a
            # `fiscal_period eq 'Q1_2026'` filter does not wrongly pull it in.
            yr = _year_from(kp_period, meta.get("fiscal_period", ""),
                            meta.get("period_label", ""),
                            meta.get("period_end_date", ""))
            mfp = _month_fiscal_period(kp_period, yr)
            if mfp:
                m["fiscal_period"] = mfp
            m["period_label"] = kp_period
        elif kp_period:
            # Freeform cumulative label such as "March YTD". Keep the page-level
            # normalized fiscal_period (e.g. Q1_2026) so the chunk stays linked
            # to the quarter and survives `fiscal_period eq 'Q1_2026'` filters;
            # expose the human label via period_label.
            m["period_label"] = kp_period
    # Brand: a per-cell KPI on a multi-brand matrix belongs to exactly ONE
    # brand. Narrow the chunk's filterable brand to that one (when it names a
    # brand present on the page) so brand filters / ranking stay correct.
    m = narrow_meta_brand(m, kp.get("brand"))

    ps = kp.get("period_scope")
    if ps and ps != "unknown":
        m["period_scope"] = ps
    mb = kp.get("measure_basis")
    if mb and mb != "unknown":
        m["measure_basis"] = mb
    elif kp.get("basis"):
        # legacy free-text basis: keep page value but record nothing wrong
        pass
    cb = kp.get("comparison_basis") or _comparison_to_basis(kp.get("comparison", ""))
    if cb:
        m["comparison_basis"] = cb
    ped = kp.get("period_end_date") or derive_period_end_date(m.get("fiscal_period", ""), {})
    if ped:
        m["period_end_date"] = ped
    return m


def base_metadata(doc: dict, page_obj: dict, brands: BrandRegistry) -> dict:
    raw_brands = page_obj.get("brands") or []
    canonical_brands, unknown_brands = brands.split_known_unknown(raw_brands)
    # If the document is brand-pinned, ensure that brand is always present
    if doc.get("brand"):
        canon = brands.canonicalize(doc["brand"]) or doc["brand"]
        if canon not in canonical_brands:
            canonical_brands.insert(0, canon)

    # brand_mentions = canonical for knowns + raw for unknowns (everything is queryable)
    mentions: list[str] = []
    seen: set[str] = set()
    for b in canonical_brands + unknown_brands:
        if b not in seen:
            seen.add(b)
            mentions.append(b)

    # TA inference: prefer page-level, else doc-level, else infer from canonical brands
    ta = page_obj.get("therapeutic_areas") or []
    if isinstance(ta, str):
        ta = [ta]
    if not ta:
        doc_ta = doc.get("therapeutic_areas") or doc.get("therapeutic_area")
        if doc_ta:
            ta = [doc_ta] if isinstance(doc_ta, str) else list(doc_ta)
    if not ta:
        inferred = [t for t in (brands.therapeutic_area(b) for b in canonical_brands) if t]
        seen_ta: set[str] = set()
        ta = [t for t in inferred if not (t in seen_ta or seen_ta.add(t))]

    # Period: prefer the page-level fiscal_period; only fall back to the document
    # value when the page did not determine one. A monthly deck legitimately
    # contains single-month, YTD and full-year-outlook pages, so blindly copying
    # the document fiscal_period onto every page was the root cause of the agent
    # confusing March, March-YTD (==Q1) and FY-outlook figures.
    page_fp = (page_obj.get("fiscal_period") or "").strip()
    if page_fp and page_fp.upper() != "UNKNOWN":
        fiscal_period = page_fp
    else:
        fiscal_period = doc.get("fiscal_period") or "UNKNOWN"

    period_end_date = (page_obj.get("period_end_date") or "").strip() \
        or derive_period_end_date(fiscal_period, doc)

    # Normalize a freeform cumulative label ("March YTD") into a calendar period
    # token ("Q1_2026") for the *filterable* fiscal_period, while preserving the
    # printed label in period_label. Without this, a query like
    # `fiscal_period eq 'Q1_2026'` (and the agent's own quarter reasoning) can
    # never reach the March-YTD == Q1 figures.
    page_label = page_obj.get("period_label", "") or ""
    if not _looks_like_fiscal_period(fiscal_period):
        yr = _year_from(fiscal_period, page_label, period_end_date,
                        doc.get("fiscal_period", ""))
        norm_fp, norm_scope, _token = normalize_ytd_label(fiscal_period or page_label, yr)
        if norm_fp:
            if not page_label:
                page_label = fiscal_period  # keep the human label we are replacing
            fiscal_period = norm_fp

    return {
        "doc_id": doc["doc_id"],
        "doc_type": doc["doc_type"],
        "fiscal_period": fiscal_period,
        "period_kind": doc.get("period_kind", ""),
        "mbr_period": doc.get("mbr_period", "") or "",
        "publication_date": doc.get("publication_date") or "",
        "geography": doc.get("geography", ""),
        "page": page_obj.get("page"),
        "title": doc.get("title") or page_obj.get("title", "") or doc["doc_id"],
        "page_kind": page_obj.get("page_kind", ""),
        # Period / basis disambiguation (page-level defaults; KPI rows specialise)
        "period_scope": page_obj.get("period_scope") or "unknown",
        "period_label": page_label,
        "period_end_date": period_end_date,
        "measure_basis": page_obj.get("measure_basis") or "unknown",
        "comparison_basis": page_obj.get("comparison_basis") or [],
        "page_role": page_obj.get("page_role") or "standard",
        "has_comments": bool(page_obj.get("has_comments", False)),
        "therapeutic_area": ta,
        "brand": canonical_brands,
        "brand_mentions": mentions,
        "compound_code": page_obj.get("compound_codes") or [],
        "lrr_stage": doc.get("lrr_stage", "") or "",
        "is_forward_looking": bool(page_obj.get("is_forward_looking", False)),
        "is_official_disclosure": bool(doc.get("is_official_disclosure", False)),
        "tags": doc.get("tags", []),
        "source_uri": doc["source"],
        "url": (doc.get("sharepoint_url") or doc["source"]) + (
            f"#page={page_obj.get('page')}" if page_obj.get("page") else ""
        ),
        "filepath": doc.get("sharepoint_url") or doc["source"],
        "extractor_version": EXTRACTOR_VERSION,
        "prompt_version": env("VISION_PROMPT_VERSION", "v1"),
    }


def emit_text(meta: dict, chunk_type: str, text: str, section: str = "",
              section_path: list[str] | None = None) -> dict:
    cid = stable_id(meta["doc_id"], str(meta["page"]), chunk_type, section, text[:80])
    out = {
        "id": cid,
        "chunk_type": chunk_type,
        "section": section,
        "section_path": section_path if section_path is not None else ([section] if section else []),
        "text": text,
        **meta,
    }
    return out


def table_to_markdown(t: dict) -> str:
    cols = t.get("columns") or []
    rows = t.get("rows") or []
    foots = t.get("footnotes") or []
    out: list[str] = []
    if t.get("caption"):
        out.append(f"**{t['caption']}**")
    if cols:
        out.append("| " + " | ".join(cols) + " |")
        out.append("| " + " | ".join(["---"] * len(cols)) + " |")
        for r in rows:
            out.append("| " + " | ".join(r) + " |")
    for fn in foots:
        out.append(f"_{fn}_")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Generic page chunker (used for everything except IR notes)
# ---------------------------------------------------------------------------

def chunks_from_page(doc: dict, page_obj: dict, brands: BrandRegistry) -> Iterable[tuple[str, dict]]:
    """Yield (target_index, chunk) tuples for one page."""
    primary = doc["primary_index"]
    pk = page_obj.get("page_kind", "")
    meta = base_metadata(doc, page_obj, brands)

    # Meta routing - cover / disclaimer / agenda / toc / references
    if pk in META_PAGE_KINDS:
        body = page_obj.get("markdown", "") or page_obj.get("title", "")
        if body.strip():
            yield "meta", emit_text(meta, "meta", body)
        return

    # Slide / prose body
    md = page_obj.get("markdown") or ""
    if md.strip():
        figures_text = ""
        for fig in page_obj.get("figures", []):
            line = f"- ({fig.get('kind','figure')}) {fig.get('caption','')}: {fig.get('description','')}"
            figures_text += "\n" + line
        body = md if not figures_text else f"{md}\n\nFigures:{figures_text}"
        if pk.startswith("slide_"):
            chunk_type = "slide"
        else:
            chunk_type = classify_text_style(body)
        yield primary, emit_text(meta, chunk_type, body)

    # Tables: one whole-table chunk + one chunk per row
    for t in page_obj.get("tables", []):
        whole = table_to_markdown(t)
        if whole.strip():
            yield primary, emit_text(meta, "table", whole, section=t.get("caption", ""))
        cols = t.get("columns") or []
        for r in t.get("rows", []):
            if not r:
                continue
            kvs = ", ".join(f"{c}={v}" for c, v in zip(cols, r))
            row_text = (f"{t.get('caption','')} - {kvs}").strip(" -")
            rmeta = narrow_meta_brand(meta, _row_brand(cols, r))
            yield primary, emit_text(rmeta, "table_row", row_text, section=t.get("caption", ""))

    # Figures -> chart / image / infographic / figure depending on kind
    for fig in page_obj.get("figures", []):
        ct = figure_chunk_type(fig.get("kind", "other"))
        text = render_figure(fig)
        yield primary, emit_text(meta, ct, text, section=fig.get("caption", ""))

    # KPI rows (one chunk per KPI for fine-grained retrieval / agent grounding)
    for kp in page_obj.get("kpis", []) or []:
        kmeta = kpi_meta_override(meta, kp)
        text = render_kpi(kp)
        aliases = kpi_period_aliases(kp, kmeta.get("fiscal_period", ""),
                                     kmeta.get("period_label", ""),
                                     kmeta.get("period_end_date", ""))
        term_aliases = kpi_term_aliases(kp)
        combined = " ".join(a for a in (aliases, term_aliases) if a)
        if combined:
            text = f"[{combined}] {text}"
        yield primary, emit_text(kmeta, "kpi_row", text, section=kp.get("name", ""))


def render_figure(fig: dict) -> str:
    dp = ", ".join(f"{d.get('label','')}={d.get('value','')}" for d in (fig.get("data_points") or []))
    axes = fig.get("axes") or {}
    axes_lines: list[str] = []
    for axis_name in ("x", "y", "size", "color"):
        ax = axes.get(axis_name) or {}
        label = ax.get("label", "")
        unit = ax.get("unit", "")
        if label or unit:
            axes_lines.append(f"  - {axis_name}: label='{label}', unit='{unit}'")
    series_lines: list[str] = []
    for s in fig.get("series", []) or []:
        name = s.get("name", "")
        for pt in s.get("points", []) or []:
            bits = ", ".join(f"{k}={pt.get(k,'')}" for k in ("label", "x", "y", "size", "color") if pt.get(k))
            series_lines.append(f"  - [{name}] {bits}")
    parts = [
        f"**{fig.get('caption','')}** ({fig.get('kind','figure')})",
        fig.get("description", ""),
    ]
    if dp:
        parts.append(f"Data points: {dp}")
    if axes_lines:
        parts.append("Axes:\n" + "\n".join(axes_lines))
    if series_lines:
        parts.append("Series:\n" + "\n".join(series_lines))
    return "\n".join(p for p in parts if p)


def render_kpi(kp: dict) -> str:
    bits = [
        kp.get("name", ""),
        f"value={kp.get('value','')} {kp.get('unit','')}".strip(),
        f"period={kp.get('period','')}",
        f"period_scope={kp.get('period_scope','')}" if kp.get("period_scope") and kp.get("period_scope") != "unknown" else "",
        f"measure_basis={kp.get('measure_basis','')}" if kp.get("measure_basis") and kp.get("measure_basis") != "unknown" else "",
        f"basis={kp.get('basis','')}" if kp.get("basis") else "",
        f"comparison={kp.get('comparison','')}" if kp.get("comparison") else "",
        f"delta={kp.get('delta_value','')} {kp.get('delta_unit','')}".strip(),
        f"brand={kp.get('brand','')}" if kp.get("brand") else "",
        f"\"{kp.get('source_quote','')}\"",
    ]
    return " | ".join(b for b in bits if b)


# ---------------------------------------------------------------------------
# IR notes path: section-aware chunking with cross-page Part state
# ---------------------------------------------------------------------------

def build_ir_part_state(pages: list[dict], detector: IRNotesSectionDetector) -> dict[int, dict]:
    """Walk pages in order, scan each line, carry the most recent Part forward.

    Returns: {page_no: {part_id, part_number, therapeutic_area, part_name}} for every page.
    """
    state: dict[int, dict] = {}
    current: dict | None = None
    for page in pages:
        page_no = int(page.get("page") or 0)
        md = page.get("markdown") or ""
        for line in md.splitlines():
            part = detector.detect_part(line)
            if part:
                current = {
                    "part_id": part.get("id", ""),
                    "part_number": detector.part_number(part),
                    "therapeutic_area": part.get("therapeutic_area"),
                    "part_name": part.get("name", ""),
                }
        state[page_no] = dict(current) if current else {}
    return state


def chunks_from_ir_page(doc: dict, page_obj: dict, brands: BrandRegistry,
                        part_info: dict, detector: IRNotesSectionDetector) -> Iterable[tuple[str, dict]]:
    """IR-notes chunker. Splits each page's markdown by `##`-style sections and
    tags every chunk with the current Part + drug subsection (if any)."""
    primary = doc["primary_index"]
    pk = page_obj.get("page_kind", "")
    meta = base_metadata(doc, page_obj, brands)
    preamble_lower = {s.lower() for s in (detector.preamble or [])}

    # Inject Part-level metadata
    if part_info:
        meta = dict(meta)
        meta["part_id"] = part_info.get("part_id", "") or ""
        meta["part_number"] = int(part_info.get("part_number") or 0)
        ta = part_info.get("therapeutic_area")
        if ta and not meta.get("therapeutic_area"):
            meta["therapeutic_area"] = [ta]

    # Meta page kinds bypass
    if pk in META_PAGE_KINDS:
        body = page_obj.get("markdown", "") or page_obj.get("title", "")
        if body.strip():
            yield "meta", emit_text(meta, "meta", body)
        return

    md = page_obj.get("markdown") or ""
    sections = split_markdown_sections(md) if md.strip() else []

    if not sections:
        # Page had no headings at all - fall back to a single prose/bullet chunk
        if md.strip():
            ct = classify_text_style(md)
            section_path = [part_info["part_name"]] if part_info.get("part_name") else []
            yield primary, emit_text(meta, ct, md, section_path=section_path)
    else:
        for heading, body in sections:
            if not body.strip() and not heading:
                continue

            # Is the heading itself a Part heading? Skip emitting it as content
            # (the part state is already applied as metadata).
            if heading and detector.detect_part(heading):
                continue

            # Is the heading a drug subsection?
            section_meta = dict(meta)
            section_path: list[str] = []
            if part_info.get("part_name"):
                section_path.append(part_info["part_name"])
            brand_canonical: str | None = None
            if heading:
                brand_canonical = brands.canonicalize(heading)
                section_path.append(heading)
            if brand_canonical:
                # Override brand list to pin this section to the specific drug
                section_meta["brand"] = [brand_canonical]
                section_meta["brand_mentions"] = list(dict.fromkeys(
                    [brand_canonical] + (section_meta.get("brand_mentions") or [])
                ))
                inferred_ta = brands.therapeutic_area(brand_canonical)
                if inferred_ta and inferred_ta not in (section_meta.get("therapeutic_area") or []):
                    section_meta["therapeutic_area"] = list(section_meta.get("therapeutic_area") or []) + [inferred_ta]
            elif heading and heading.lower() not in preamble_lower and _looks_brand_like(heading):
                # Unregistered heading that looks like a drug name -> add to mentions
                section_meta["brand_mentions"] = list(dict.fromkeys(
                    (section_meta.get("brand_mentions") or []) + [heading.strip()]
                ))

            # Combine heading + body so the chunk text is self-describing
            combined = (f"## {heading}\n\n{body}".strip() if heading else body.strip())
            ct = classify_text_style(combined)
            yield primary, emit_text(
                section_meta,
                ct,
                combined,
                section=heading,
                section_path=section_path,
            )

    # Tables, figures, KPIs from this page (same as generic path)
    for t in page_obj.get("tables", []):
        whole = table_to_markdown(t)
        if whole.strip():
            yield primary, emit_text(meta, "table", whole, section=t.get("caption", ""))
        cols = t.get("columns") or []
        for r in t.get("rows", []):
            if not r:
                continue
            kvs = ", ".join(f"{c}={v}" for c, v in zip(cols, r))
            row_text = (f"{t.get('caption','')} - {kvs}").strip(" -")
            rmeta = narrow_meta_brand(meta, _row_brand(cols, r))
            yield primary, emit_text(rmeta, "table_row", row_text, section=t.get("caption", ""))

    for fig in page_obj.get("figures", []):
        ct = figure_chunk_type(fig.get("kind", "other"))
        yield primary, emit_text(meta, ct, render_figure(fig), section=fig.get("caption", ""))

    for kp in page_obj.get("kpis", []) or []:
        yield primary, emit_text(kpi_meta_override(meta, kp), "kpi_row", render_kpi(kp), section=kp.get("name", ""))


# ---------------------------------------------------------------------------
# Sidecar + main
# ---------------------------------------------------------------------------

def write_kpis(doc: dict, vision_pages: list[dict], kpi_dir: Path) -> int:
    kpi_dir.mkdir(parents=True, exist_ok=True)
    all_kpis: list[dict] = []
    for p in vision_pages:
        for kp in p.get("kpis", []) or []:
            kp = dict(kp)
            kp["page"] = p.get("page")
            all_kpis.append(kp)
    if not all_kpis:
        return 0
    out = {
        "doc_id": doc["doc_id"],
        "doc_type": doc["doc_type"],
        "fiscal_period": doc.get("fiscal_period"),
        "publication_date": doc.get("publication_date"),
        "source_file": doc["source"],
        "extractor_version": EXTRACTOR_VERSION,
        "prompt_version": env("VISION_PROMPT_VERSION", "v1"),
        "kpis": all_kpis,
    }
    (kpi_dir / f"{doc['doc_id']}.kpi.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(all_kpis)


def main() -> None:
    p = paths()
    chunks_dir = p["chunks"]
    chunks_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest()
    brands = BrandRegistry(manifest)
    ir_detector = IRNotesSectionDetector(manifest)
    print(f"Brand registry: {brands.all_canonical}")
    print(f"IR parts loaded: {[p.get('id') for p in ir_detector.parts]}")

    index_files: dict[str, Path] = {name: chunks_dir / f"{name}.jsonl" for name in manifest["indices"]}

    # Preserve chunks from docs not in scope (so partial runs don't wipe other docs)
    existing_by_index: dict[str, list[dict]] = {name: [] for name in manifest["indices"]}
    docs_in_scope = {d["doc_id"] for d in docs_to_process(manifest)}
    for name, path in index_files.items():
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("doc_id") not in docs_in_scope:
                    existing_by_index[name].append(rec)

    new_by_index: dict[str, list[dict]] = {name: [] for name in manifest["indices"]}
    summary = []
    for doc in docs_to_process(manifest):
        vision_dir = p["vision"] / doc["doc_id"]
        if not vision_dir.exists():
            print(f"SKIP, no vision JSON: {vision_dir}")
            continue
        pages: list[dict] = [
            json.loads(f.read_text(encoding="utf-8"))
            for f in sorted(vision_dir.glob("page*.json"))
        ]

        # Pick the right per-doc path
        if doc.get("doc_type") == "ir_notes":
            ir_state = build_ir_part_state(pages, ir_detector)
            page_iter = (
                (page_obj, ir_state.get(int(page_obj.get("page") or 0), {}))
                for page_obj in pages
            )
            chunk_iter: Iterable[tuple[str, dict]] = (
                pair
                for page_obj, info in page_iter
                for pair in chunks_from_ir_page(doc, page_obj, brands, info, ir_detector)
            )
        else:
            chunk_iter = (
                pair
                for page_obj in pages
                for pair in chunks_from_page(doc, page_obj, brands)
            )

        emitted = 0
        for target, chunk in chunk_iter:
            if target not in new_by_index:
                new_by_index[target] = []
            new_by_index[target].append(chunk)
            emitted += 1

        n_kpis = write_kpis(doc, pages, p["kpi"])
        summary.append({"doc_id": doc["doc_id"], "pages": len(pages), "chunks": emitted, "kpis": n_kpis})

    for name, path in index_files.items():
        merged = existing_by_index[name] + new_by_index[name]
        with path.open("w", encoding="utf-8") as f:
            for c in merged:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")

    print(json.dumps({
        "summary_per_doc": summary,
        "totals_per_index": {name: len(existing_by_index[name] + new_by_index[name]) for name in index_files},
    }, indent=2))


if __name__ == "__main__":
    main()

