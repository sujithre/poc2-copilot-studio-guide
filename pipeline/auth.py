"""Centralized Azure auth using DefaultAzureCredential (az login)."""
from __future__ import annotations
from functools import lru_cache

from azure.identity import DefaultAzureCredential, get_bearer_token_provider

AOAI_SCOPE = "https://cognitiveservices.azure.com/.default"
SEARCH_SCOPE = "https://search.azure.com/.default"


@lru_cache(maxsize=1)
def credential() -> DefaultAzureCredential:
    return DefaultAzureCredential(exclude_interactive_browser_credential=False)


@lru_cache(maxsize=1)
def aoai_token_provider():
    return get_bearer_token_provider(credential(), AOAI_SCOPE)
