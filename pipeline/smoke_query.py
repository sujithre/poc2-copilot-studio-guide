"""Smoke-test retrieval against the indexed POC2 chunks.

Usage:
  python smoke_query.py "How is Pluvicto Q1 2026 doing?"
  python smoke_query.py "March 2026 Net Sales by brand" --index financial_results --top 5
  python smoke_query.py "FY 2026 guidance" --filter "is_forward_looking eq true"
  python smoke_query.py "Kisqali growth" --index external_messages --filter "brand/any(b: b eq 'Kisqali')"

Defaults:
  --index    external_messages   (good general-purpose default)
  --top      5
  --semantic on   (uses semantic ranker if the service tier supports it)
"""
from __future__ import annotations
import argparse
import sys

from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from openai import AzureOpenAI

from auth import aoai_token_provider, credential
from common import env, load_manifest


def embed(client: AzureOpenAI, deployment: str, text: str) -> list[float]:
    resp = client.embeddings.create(model=deployment, input=[text])
    return resp.data[0].embedding


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="Natural-language question")
    ap.add_argument("--index", default="external_messages", help="Logical index from manifest.json")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--filter", default=None, help="OData $filter expression")
    ap.add_argument("--no-semantic", action="store_true")
    args = ap.parse_args()

    manifest = load_manifest()
    if args.index not in manifest["indices"]:
        print(f"Unknown index '{args.index}'. Choose from: {list(manifest['indices'])}", file=sys.stderr)
        return 2
    azure_index = manifest["indices"][args.index]["azure_index"]

    aoai = AzureOpenAI(
        azure_endpoint=env("AZURE_OPENAI_ENDPOINT", required=True),
        azure_ad_token_provider=aoai_token_provider(),
        api_version=env("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )
    embed_deployment = env("AZURE_OPENAI_EMBED_DEPLOYMENT", required=True)
    vec = embed(aoai, embed_deployment, args.query)

    sc = SearchClient(
        endpoint=env("AZURE_SEARCH_ENDPOINT", required=True),
        index_name=azure_index,
        credential=credential(),
    )

    # Newer azure-search-documents SDKs renamed the param to `k`. Try the new
    # name first; fall back to the old `k_nearest_neighbors` for older SDKs.
    try:
        vec_q = VectorizedQuery(vector=vec, k=args.top, fields="vector")
    except TypeError:
        vec_q = VectorizedQuery(vector=vec, k_nearest_neighbors=args.top, fields="vector")

    kwargs = dict(
        search_text=args.query,            # hybrid: BM25 + vector
        vector_queries=[vec_q],
        top=args.top,
        filter=args.filter,
        select=[
            "id", "doc_id", "page", "chunk_type", "fiscal_period", "period_kind",
            "page_kind", "page_role", "title", "brand", "brand_mentions", "therapeutic_area", "part_id",
            "is_forward_looking", "source_uri", "url", "chunk",
        ],
    )
    if not args.no_semantic:
        kwargs.update(query_type="semantic", semantic_configuration_name="default-semantic")

    try:
        results = list(sc.search(**kwargs))
    except Exception as e:
        if not args.no_semantic and "semantic" in str(e).lower():
            print("(semantic ranker unavailable; falling back to hybrid BM25 + vector)\n", file=sys.stderr)
            kwargs.pop("query_type", None)
            kwargs.pop("semantic_configuration_name", None)
            results = list(sc.search(**kwargs))
        else:
            raise

    print(f"\nQuery:  {args.query}")
    print(f"Index:  {azure_index}   filter={args.filter!r}   top={args.top}\n")
    if not results:
        print("(no results)")
        return 0

    for i, r in enumerate(results, 1):
        score = r.get("@search.score")
        rerank = r.get("@search.reranker_score")
        text = (r.get("chunk") or "").replace("\n", " ").strip()
        snippet = text[:280] + ("..." if len(text) > 280 else "")
        print(f"#{i}  score={score:.3f}" + (f"  rerank={rerank:.3f}" if rerank is not None else ""))
        print(f"    [{r.get('chunk_type')}] {r.get('doc_id')} p{r.get('page')}  fp={r.get('fiscal_period')}  fwd={r.get('is_forward_looking')}")
        print(f"    role : page_role={r.get('page_role')}  page_kind={r.get('page_kind')}")
        if r.get("title"):
            print(f"    title: {r['title']}")
        if r.get("brand"):
            print(f"    brand: {', '.join(r['brand'])}")
        if r.get("part_id"):
            print(f"    part : {r['part_id']}")
        print(f"    {snippet}")
        print(f"    source: {r.get('source_uri')}#page={r.get('page')}")
        print(f"    url   : {r.get('url')}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
