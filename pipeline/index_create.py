"""Create one Azure AI Search index per logical index in the POC2 manifest.

Schema is identical for all indices (same chunk shape) - only the name differs.
Honors manifest.indices[*].azure_index verbatim (e.g., 'finsight-us-financial-results').
"""
from __future__ import annotations
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
)

from auth import credential
from common import env, load_manifest

EMBED_DIM = 3072  # text-embedding-3-large default


def index_def(name: str, aoai_endpoint: str, embed_deployment: str, embed_model: str) -> SearchIndex:
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
                    ],
                ),
            )
        ]
    )

    return SearchIndex(name=name, fields=fields, vector_search=vector_search, semantic_search=semantic_search)


def main() -> None:
    endpoint = env("AZURE_SEARCH_ENDPOINT", required=True)
    aoai_endpoint = env("AZURE_OPENAI_ENDPOINT", required=True)
    embed_deployment = env("AZURE_OPENAI_EMBED_DEPLOYMENT", required=True)
    embed_model = env("AZURE_OPENAI_EMBED_MODEL", "text-embedding-3-large")
    client = SearchIndexClient(endpoint=endpoint, credential=credential())

    manifest = load_manifest()
    created = []
    for logical, cfg in manifest["indices"].items():
        name = cfg["azure_index"]  # honor manifest verbatim (e.g., finsight-us-...)
        idx = index_def(name, aoai_endpoint, embed_deployment, embed_model)
        client.create_or_update_index(idx)
        created.append({"logical": logical, "azure_index": name})
    print(json.dumps({"created_or_updated": created}, indent=2))


if __name__ == "__main__":
    main()
