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

EXTRACTOR_VERSION = "0.1.0"

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

    return {
        "doc_id": doc["doc_id"],
        "doc_type": doc["doc_type"],
        "fiscal_period": page_obj.get("fiscal_period") or doc.get("fiscal_period") or "UNKNOWN",
        "period_kind": doc.get("period_kind", ""),
        "mbr_period": doc.get("mbr_period", "") or "",
        "publication_date": doc.get("publication_date") or "",
        "geography": doc.get("geography", ""),
        "page": page_obj.get("page"),
        "title": page_obj.get("title", ""),
        "page_kind": page_obj.get("page_kind", ""),
        "therapeutic_area": ta,
        "brand": canonical_brands,
        "brand_mentions": mentions,
        "compound_code": page_obj.get("compound_codes") or [],
        "lrr_stage": doc.get("lrr_stage", "") or "",
        "is_forward_looking": bool(page_obj.get("is_forward_looking", False)),
        "is_official_disclosure": bool(doc.get("is_official_disclosure", False)),
        "tags": doc.get("tags", []),
        "source_uri": doc["source"],
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
            yield primary, emit_text(meta, "table_row", row_text, section=t.get("caption", ""))

    # Figures -> chart / image / infographic / figure depending on kind
    for fig in page_obj.get("figures", []):
        ct = figure_chunk_type(fig.get("kind", "other"))
        text = render_figure(fig)
        yield primary, emit_text(meta, ct, text, section=fig.get("caption", ""))

    # KPI rows (one chunk per KPI for fine-grained retrieval / agent grounding)
    for kp in page_obj.get("kpis", []) or []:
        text = render_kpi(kp)
        yield primary, emit_text(meta, "kpi_row", text, section=kp.get("name", ""))


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
            yield primary, emit_text(meta, "table_row", row_text, section=t.get("caption", ""))

    for fig in page_obj.get("figures", []):
        ct = figure_chunk_type(fig.get("kind", "other"))
        yield primary, emit_text(meta, ct, render_figure(fig), section=fig.get("caption", ""))

    for kp in page_obj.get("kpis", []) or []:
        yield primary, emit_text(meta, "kpi_row", render_kpi(kp), section=kp.get("name", ""))


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

