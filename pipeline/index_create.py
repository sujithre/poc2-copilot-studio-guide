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
    SemanticConfiguration,
    SemanticPrioritizedFields,
    SemanticField,
    SemanticSearch,
)

from auth import credential
from common import env, load_manifest

EMBED_DIM = 3072  # text-embedding-3-large default


def index_def(name: str) -> SearchIndex:
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SearchableField(name="text", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchField(
            name="vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBED_DIM,
            vector_search_profile_name="default-vec-profile",
        ),
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
        SimpleField(name="extractor_version", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="prompt_version",    type=SearchFieldDataType.String, filterable=True),
    ]

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(name="default-hnsw", parameters=HnswParameters(metric="cosine")),
        ],
        profiles=[
            VectorSearchProfile(name="default-vec-profile", algorithm_configuration_name="default-hnsw"),
        ],
    )

    semantic_search = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name="default-semantic",
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    content_fields=[SemanticField(field_name="text")],
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
    client = SearchIndexClient(endpoint=endpoint, credential=credential())

    manifest = load_manifest()
    created = []
    for logical, cfg in manifest["indices"].items():
        name = cfg["azure_index"]  # honor manifest verbatim (e.g., finsight-us-...)
        idx = index_def(name)
        client.create_or_update_index(idx)
        created.append({"logical": logical, "azure_index": name})
    print(json.dumps({"created_or_updated": created}, indent=2))


if __name__ == "__main__":
    main()
