"""Rasterize each PDF page to a PNG. Cached by file mtime + DPI.

Reads source PDF paths from manifest.documents[*].source (resolved against POC2/).
Writes PNGs to POC2/PDF_pages/<doc_id>/page0001.png, plus a _hashes.json sidecar.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

import fitz  # pymupdf
from tqdm import tqdm

from common import env, load_manifest, docs_to_process, paths

# Silence benign MuPDF warnings (e.g. "No common ancestor in structure tree")
# that appear for PDFs exported from PowerPoint/Word with malformed accessibility
# tags. Pages still render correctly; only the stderr noise is suppressed.
fitz.TOOLS.mupdf_display_errors(False)


def page_hash(pix: fitz.Pixmap) -> str:
    return hashlib.sha256(pix.tobytes("png")).hexdigest()


def rasterize_doc(pdf_path: Path, out_dir: Path, dpi: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    hashes_file = out_dir / "_hashes.json"
    existing = json.loads(hashes_file.read_text(encoding="utf-8")) if hashes_file.exists() else {}

    doc = fitz.open(pdf_path)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    new_hashes: dict[str, str] = {}
    n_new, n_cached = 0, 0
    for i, page in enumerate(tqdm(doc, desc=f"rasterize {pdf_path.name}", unit="pg"), start=1):
        out_path = out_dir / f"page{i:04d}.png"
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        h = page_hash(pix)
        new_hashes[out_path.name] = h
        if existing.get(out_path.name) == h and out_path.exists():
            n_cached += 1
            continue
        pix.save(out_path)
        n_new += 1
    doc.close()

    hashes_file.write_text(json.dumps(new_hashes, indent=2), encoding="utf-8")
    return {"pdf": pdf_path.name, "pages": len(new_hashes), "new": n_new, "cached": n_cached, "out_dir": str(out_dir)}


def main() -> None:
    p = paths()
    dpi = int(env("RASTER_DPI", "250"))
    manifest = load_manifest()
    results = []
    for doc in docs_to_process(manifest):
        # source is relative to POC2/ (e.g. "docs/foo.pdf")
        pdf_path = p["root"] / doc["source"]
        out_dir = p["pages"] / doc["doc_id"]
        if not pdf_path.exists():
            print(f"SKIP missing: {pdf_path}")
            continue
        if pdf_path.suffix.lower() != ".pdf":
            print(f"SKIP non-PDF (convert .pptx/.docx to PDF first): {pdf_path}")
            continue
        results.append(rasterize_doc(pdf_path, out_dir, dpi))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
