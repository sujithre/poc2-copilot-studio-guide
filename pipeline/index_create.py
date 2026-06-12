"""Create one Azure AI Search index per logical index in the POC2 manifest.

Schema is identical for all indices (same chunk shape) - only the name differs.
Honors manifest.indices[*].azure_index verbatim (e.g., 'finsight-us-financial-results').
"""
from __future__ import annotations
import argparse
import json

from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SimpleField,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    VectorSearch,
    HnswAlgorithmConfiguration,
    HnswParameters,
    VectorSearchProfile,
    AzureOpenAIVectorizer,
    AzureOpenAIVectorizerParameters,
    SemanticConfiguration,
    SemanticPrioritizedFields,
    SemanticField,
    SemanticSearch,
    ScoringProfile,
    FreshnessScoringFunction,
    FreshnessScoringParameters,
    MagnitudeScoringFunction,
    MagnitudeScoringParameters,
)

from auth import credential
from common import env, load_manifest

EMBED_DIM = 3072  # text-embedding-3-large default


def index_def(name: str, aoai_endpoint: str, embed_deployment: str, embed_model: str,
              logical: str = "") -> SearchIndex:
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        # Copilot Studio's AI Search knowledge source auto-detects the field named `chunk`
        # as the citation snippet body (matches the portal Import-and-vectorize wizard convention).
        SearchableField(name="chunk", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchField(
            name="vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBED_DIM,
            vector_search_profile_name="default-vec-profile",
        ),
        # NOTE: vectorizer is configured on the profile below so Foundry's
        # AzureAISearchTool (vector_semantic_hybrid) can embed the user query
        # server-side. Without it the agent tool errors with
        # 'vector_semantic_hybrid requires a vector field with integrated vectorizer'.
        # ---- doc-level
        SimpleField(name="doc_id",            type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="doc_type",          type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="fiscal_period",     type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="period_kind",       type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="mbr_period",        type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="publication_date",  type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="geography",         type=SearchFieldDataType.String, filterable=True, facetable=True),
        # ---- page-level
        SimpleField(name="page",              type=SearchFieldDataType.Int32,  filterable=True, sortable=True),
        SearchableField(name="title",         type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SimpleField(name="page_kind",         type=SearchFieldDataType.String, filterable=True, facetable=True),
        # ---- period / basis disambiguation (lets the agent separate March vs
        #      March-YTD(==Q1) vs FY-outlook, and actual vs LO vs target cells)
        SimpleField(name="period_scope",      type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="period_label",      type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="period_end_date",   type=SearchFieldDataType.String, filterable=True, sortable=True),
        # DateTimeOffset twin of period_end_date, used by the freshness scoring
        # profile for recency boosting (latest period wins by default).
        SimpleField(name="recency_date",      type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SimpleField(name="measure_basis",     type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchField(name="comparison_basis",  type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True, facetable=True),
        SimpleField(name="page_role",         type=SearchFieldDataType.String, filterable=True, facetable=True),
        # Retrieval precedence magnitude (set by the chunker from page_role):
        # brand_matrix grids get a higher value so the authoritative brand x
        # period tables float above narrower brand-dedicated slides. Boosted by
        # the 'authority' magnitude function in the scoring profile below.
        SimpleField(name="authority_boost",   type=SearchFieldDataType.Int32, filterable=True, sortable=True),
        SimpleField(name="has_comments",      type=SearchFieldDataType.Boolean, filterable=True),
        # ---- chunk-level
        SimpleField(name="chunk_type",        type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="section",           type=SearchFieldDataType.String, filterable=True),
        SearchField(name="section_path",      type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True),
        SimpleField(name="part_id",           type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="part_number",       type=SearchFieldDataType.Int32,  filterable=True, sortable=True),
        # ---- product / TA
        SearchField(name="therapeutic_area",  type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True, facetable=True),
        SearchField(name="brand",             type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True, facetable=True),
        SearchField(name="brand_mentions",    type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True, facetable=True),
        SearchField(name="compound_code",     type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True, facetable=True),
        SimpleField(name="lrr_stage",         type=SearchFieldDataType.String, filterable=True, facetable=True),
        # ---- flags + provenance
        SimpleField(name="is_forward_looking",     type=SearchFieldDataType.Boolean, filterable=True),
        SimpleField(name="is_official_disclosure", type=SearchFieldDataType.Boolean, filterable=True),
        SearchField(name="tags",              type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True, facetable=True),
        SimpleField(name="source_uri",        type=SearchFieldDataType.String, filterable=True),
        # Copilot Studio AI Search knowledge source auto-detects these citation fields:
        #   - `metadata_storage_path` -> citation URL (highest priority per CS docs)
        #   - `title`                  -> citation chip title
        #   - `chunk`                  -> citation snippet body (wizard convention)
        # `url` / `filepath` are kept for backwards compatibility and for the agent instructions
        # which emit `[<title>, p.<page>](<url>)` markdown links.
        SimpleField(name="metadata_storage_path", type=SearchFieldDataType.String),
        SimpleField(name="url",               type=SearchFieldDataType.String),
        SimpleField(name="filepath",          type=SearchFieldDataType.String),
        SimpleField(name="extractor_version", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="prompt_version",    type=SearchFieldDataType.String, filterable=True),
    ]

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(name="default-hnsw", parameters=HnswParameters(metric="cosine")),
        ],
        vectorizers=[
            AzureOpenAIVectorizer(
                vectorizer_name="default-vectorizer",
                parameters=AzureOpenAIVectorizerParameters(
                    resource_url=aoai_endpoint,
                    deployment_name=embed_deployment,
                    model_name=embed_model,
                    # No api_key -> service uses its own managed identity
                    # (Cognitive Services OpenAI User on the AOAI account).
                ),
            ),
        ],
        profiles=[
            VectorSearchProfile(
                name="default-vec-profile",
                algorithm_configuration_name="default-hnsw",
                vectorizer_name="default-vectorizer",
            ),
        ],
    )

    semantic_search = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name="default-semantic",
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    content_fields=[SemanticField(field_name="chunk")],
                    keywords_fields=[
                        SemanticField(field_name="brand"),
                        SemanticField(field_name="therapeutic_area"),
                        SemanticField(field_name="period_label"),
                    ],
                ),
            )
        ]
    )

    # Gentle recency boost: documents covering a more recent period_end_date
    # score higher, so when two files report the same figure the newer one wins
    # by default (the agent can still pin an older period via a filter).
    scoring_profiles = [
        ScoringProfile(
            name="recency-boost",
            functions=[
                FreshnessScoringFunction(
                    field_name="recency_date",
                    boost=2.0,
                    parameters=FreshnessScoringParameters(boosting_duration="P730D"),
                    interpolation="linear",
                ),
                # Precedence boost: float the authoritative brand x period grids
                # (authority_boost=3 for brand_matrix, 1 for narrative, 0 else)
                # above narrower brand-dedicated slides for KPI questions. Keyed
                # on content-derived page_role, so it survives the monthly deck
                # refresh with no page-number maintenance. Tune `boost` then
                # rebuild + smoke-test if it over/under-fires.
                MagnitudeScoringFunction(
                    field_name="authority_boost",
                    boost=4.0,
                    parameters=MagnitudeScoringParameters(
                        boosting_range_start=0,
                        boosting_range_end=3,
                        should_boost_beyond_range_by_constant=True,
                    ),
                    interpolation="linear",
                ),
            ],
        ),
        # External-messages precedence: SOURCE CLASS, not date. The IR Notes
        # message (authority_boost=3) must win over the Quarterly Update
        # (authority_boost=2) every time - but the Quarterly Update is the NEWER
        # doc, so a freshness function would invert that. This profile drops
        # freshness entirely and keeps only the authority magnitude, so
        # IR > Quarterly is deterministic. Default ONLY for external_messages;
        # the financial index is untouched and keeps 'recency-boost'.
        ScoringProfile(
            name="external-authority",
            functions=[
                MagnitudeScoringFunction(
                    field_name="authority_boost",
                    boost=8.0,
                    parameters=MagnitudeScoringParameters(
                        boosting_range_start=0,
                        boosting_range_end=3,
                        should_boost_beyond_range_by_constant=True,
                    ),
                    interpolation="linear",
                ),
            ],
        ),
    ]

    # External messaging is ordered by source class (IR > Quarterly), so it uses
    # the freshness-free profile. Every other index keeps recency (latest wins).
    default_profile = "external-authority" if logical == "external_messages" else "recency-boost"

    return SearchIndex(
        name=name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
        scoring_profiles=scoring_profiles,
        default_scoring_profile=default_profile,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Create/update POC2 search indices.")
    parser.add_argument(
        "--drop", action="store_true",
        help="Delete each index before recreating it (clean rebuild; removes "
             "stale orphan documents left behind by content-keyed upserts).",
    )
    parser.add_argument(
        "--only", default="",
        help="Restrict to a single logical index (e.g. financial_results).",
    )
    args = parser.parse_args()

    endpoint = env("AZURE_SEARCH_ENDPOINT", required=True)
    aoai_endpoint = env("AZURE_OPENAI_ENDPOINT", required=True)
    embed_deployment = env("AZURE_OPENAI_EMBED_DEPLOYMENT", required=True)
    embed_model = env("AZURE_OPENAI_EMBED_MODEL", "text-embedding-3-large")
    client = SearchIndexClient(endpoint=endpoint, credential=credential())

    manifest = load_manifest()
    created = []
    for logical, cfg in manifest["indices"].items():
        if args.only and logical != args.only:
            continue
        name = cfg["azure_index"]  # honor manifest verbatim (e.g., finsight-us-...)
        if args.drop:
            try:
                client.delete_index(name)
            except Exception:
                pass  # index may not exist yet
        idx = index_def(name, aoai_endpoint, embed_deployment, embed_model, logical=logical)
        client.create_or_update_index(idx)
        created.append({"logical": logical, "azure_index": name, "dropped": args.drop})
    print(json.dumps({"created_or_updated": created}, indent=2))


if __name__ == "__main__":
    main()
