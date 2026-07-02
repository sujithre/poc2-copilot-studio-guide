"""Embed chunks and upload to the matching Azure AI Search index."""
from __future__ import annotations
import argparse
import json

from azure.search.documents import SearchClient
from openai import AzureOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from auth import aoai_token_provider, credential
from common import env, load_manifest, paths


def make_aoai() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=env("AZURE_OPENAI_ENDPOINT", required=True),
        azure_ad_token_provider=aoai_token_provider(),
        api_version=env("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20))
def embed_batch(client: AzureOpenAI, deployment: str, texts: list[str]) -> list[list[float]]:
    resp = client.embeddings.create(model=deployment, input=texts)
    return [d.embedding for d in resp.data]


def _to_recency_date(value: str) -> str | None:
    """Coerce a 'YYYY-MM-DD' (or 'YYYY-MM') period_end_date string to an ISO 8601
    DateTimeOffset that Azure Search accepts. Returns None when unparseable so the
    freshness scoring function simply skips the document."""
    v = (value or "").strip()
    if not v:
        return None
    import re as _re
    m = _re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", v)
    if not m:
        return None
    y, mo = m.group(1), m.group(2)
    d = m.group(3) or "01"
    return f"{y}-{mo}-{d}T00:00:00Z"


def chunk_to_doc(rec: dict, vector: list[float]) -> dict:
    """Map a chunk JSON record to the Azure Search field shape."""
    # Embed a clickable SOURCE link INTO the content the generative model reads.
    # Copilot Studio feeds the `chunk` field to the model but NOT the `url`
    # metadata field (that only powers the native citation chip, which is lost
    # across the multi-agent child->parent hop). Appending the link here lets
    # each child agent emit a deterministic `Sources: [title](url)` line.
    # NOTE: the embedding `vector` is computed upstream from the CLEAN `text`
    # (see main()), so semantic ranking is unaffected; only keyword search sees
    # the URL tokens - an acceptable, incremental cost since the already-
    # searchable `title` field carries the same document-name words.
    body = rec.get("text", "") or rec.get("title", "")
    src_url = rec.get("url", "") or rec.get("source_uri", "")
    if src_url:
        body = f"{body}\n\nSOURCE: [{rec.get('title', '')}]({src_url})"
    return {
        "id": rec["id"],
        "chunk": body,
        "vector": vector,
        # doc-level
        "doc_id": rec.get("doc_id", ""),
        "doc_type": rec.get("doc_type", ""),
        "fiscal_period": rec.get("fiscal_period", ""),
        "period_kind": rec.get("period_kind", "") or "",
        "mbr_period": rec.get("mbr_period", "") or "",
        "publication_date": rec.get("publication_date") or "",
        "geography": rec.get("geography", "") or "",
        # page-level
        "page": int(rec.get("page") or 0),
        "title": rec.get("title", ""),
        "page_kind": rec.get("page_kind", ""),
        # period / basis disambiguation
        "period_scope": rec.get("period_scope", "") or "unknown",
        "period_label": rec.get("period_label", "") or "",
        "period_end_date": rec.get("period_end_date", "") or "",
        "recency_date": _to_recency_date(rec.get("period_end_date", "") or rec.get("publication_date", "")),
        "measure_basis": rec.get("measure_basis", "") or "unknown",
        "comparison_basis": rec.get("comparison_basis") or [],
        "page_role": rec.get("page_role", "") or "standard",
        "authority_boost": int(rec.get("authority_boost") or 0),
        "has_comments": bool(rec.get("has_comments", False)),
        # chunk-level
        "chunk_type": rec.get("chunk_type", ""),
        "section": rec.get("section", "") or "",
        "section_path": rec.get("section_path") or [],
        "part_id": rec.get("part_id", "") or "",
        "part_number": int(rec.get("part_number") or 0) if rec.get("part_number") not in (None, "") else 0,
        # product / TA
        "therapeutic_area": rec.get("therapeutic_area") or [],
        "brand": rec.get("brand") or [],
        "brand_mentions": rec.get("brand_mentions") or [],
        "compound_code": rec.get("compound_code") or [],
        "lrr_stage": rec.get("lrr_stage", "") or "",
        # flags + provenance
        "is_forward_looking": bool(rec.get("is_forward_looking", False)),
        "is_official_disclosure": bool(rec.get("is_official_disclosure", False)),
        "tags": rec.get("tags") or [],
        "source_uri": rec.get("source_uri", ""),
        "metadata_storage_path": rec.get("url", "") or rec.get("source_uri", ""),
        "url": rec.get("url", "") or rec.get("source_uri", ""),
        "filepath": rec.get("filepath", "") or rec.get("source_uri", ""),
        "extractor_version": rec.get("extractor_version", ""),
        "prompt_version": rec.get("prompt_version", ""),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=None,
                    help="Limit to specific logical index names (repeatable). Default: all.")
    args = ap.parse_args()

    p = paths()
    manifest = load_manifest()
    aoai_deployment = env("AZURE_OPENAI_EMBED_DEPLOYMENT", required=True)
    search_endpoint = env("AZURE_SEARCH_ENDPOINT", required=True)
    batch = int(env("EMBED_BATCH_SIZE", "16"))
    aoai = make_aoai()
    cred = credential()

    summary = []
    for logical, cfg in manifest["indices"].items():
        if args.only and logical not in args.only:
            continue
        jsonl = p["chunks"] / f"{logical}.jsonl"
        if not jsonl.exists():
            print(f"SKIP, no chunks: {jsonl}")
            continue
        records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not records:
            continue

        sc = SearchClient(endpoint=search_endpoint, index_name=cfg["azure_index"], credential=cred)
        uploaded = 0
        skipped_empty = 0
        for i in tqdm(range(0, len(records), batch), desc=f"upload {logical}", unit="batch"):
            chunk = records[i:i + batch]
            chunk = [r for r in chunk if (r.get("text") or r.get("title") or "").strip()]
            skipped_in_batch = (len(records[i:i + batch]) - len(chunk))
            skipped_empty += skipped_in_batch
            if not chunk:
                continue
            texts = [r.get("text", "") or r.get("title", "") for r in chunk]
            vectors = embed_batch(aoai, aoai_deployment, texts)
            docs = [chunk_to_doc(r, v) for r, v in zip(chunk, vectors)]
            sc.upload_documents(documents=docs)
            uploaded += len(docs)
        summary.append({"index": cfg["azure_index"], "uploaded": uploaded, "skipped_empty": skipped_empty})

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
