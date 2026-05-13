"""Provision FinSight US Foundry agents (one per Azure AI Search index).

For each spec in agents/specs.py:
  - looks up the matching Azure index name from manifest.json
  - if an agent with the same name already exists, deletes it
  - creates a new versioned agent wired to that index via AzureAISearchTool

Auth: AzureCliCredential (run `az login` first).

Env (.env auto-loaded from POC2/.env or workspace .env):
    AZURE_AI_PROJECT_ENDPOINT
    FOUNDRY_MODEL_DEPLOYMENT          (e.g. gpt-4.1)
    FOUNDRY_SEARCH_CONNECTION_ID      (AI Search project connection id/name)
    FOUNDRY_AGENT_PREFIX              (default: 'finsight-us')
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make sibling pipeline modules importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AISearchIndexResource,
    AzureAISearchQueryType,
    AzureAISearchTool,
    AzureAISearchToolResource,
    PromptAgentDefinition,
)

from common import env, load_manifest
from agents.specs import AGENT_SPECS, AgentSpec


def _query_type(name: str) -> AzureAISearchQueryType:
    return getattr(AzureAISearchQueryType, name.upper())


def _build_tool(spec: AgentSpec, index_name: str, conn_id: str) -> AzureAISearchTool:
    return AzureAISearchTool(
        azure_ai_search=AzureAISearchToolResource(
            indexes=[
                AISearchIndexResource(
                    project_connection_id=conn_id,
                    index_name=index_name,
                    query_type=_query_type(spec.query_type),
                    top_k=spec.top_k,
                )
            ]
        )
    )


def _try_delete(project_client: AIProjectClient, agent_name: str) -> None:
    try:
        existing = project_client.agents.get(agent_name)
    except Exception as ex:  # noqa: BLE001
        msg = str(ex).lower()
        if "404" in msg or "resourcenotfound" in msg or "not_found" in msg or "doesn't exist" in msg:
            return
        print(f"WARN: could not query existing agent '{agent_name}': {ex}", file=sys.stderr)
        return
    if existing is None:
        return
    print(f"  removing existing agent '{agent_name}'")
    try:
        project_client.agents.delete(agent_name)
    except Exception as ex:  # noqa: BLE001
        print(f"WARN: could not delete '{agent_name}': {ex}", file=sys.stderr)


def main() -> int:
    project_endpoint = env("AZURE_AI_PROJECT_ENDPOINT", required=True)
    model_deployment = env("FOUNDRY_MODEL_DEPLOYMENT", required=True)
    conn_input      = env("FOUNDRY_SEARCH_CONNECTION_ID", required=True)
    prefix           = env("FOUNDRY_AGENT_PREFIX", "finsight-us")

    manifest = load_manifest()
    indices = manifest["indices"]

    summary = []
    with (
        AzureCliCredential() as credential,
        AIProjectClient(endpoint=project_endpoint, credential=credential) as project_client,
    ):
        # Resolve the connection name to its full ARM id (the tool requires the id).
        if conn_input.startswith("/subscriptions/"):
            conn_id = conn_input
        else:
            conn = project_client.connections.get(conn_input)
            conn_id = conn.id
            print(f"Resolved connection '{conn_input}' -> {conn_id}")

        for spec in AGENT_SPECS:
            if spec.index_logical not in indices:
                print(f"SKIP {spec.role}: index '{spec.index_logical}' not in manifest")
                continue
            azure_index = indices[spec.index_logical]["azure_index"]
            agent_name = f"{prefix}-{spec.role}"

            print(f"\n>> {agent_name}  -> index {azure_index}")
            _try_delete(project_client, agent_name)

            tool = _build_tool(spec, azure_index, conn_id)
            agent = project_client.agents.create_version(
                agent_name=agent_name,
                definition=PromptAgentDefinition(
                    model=model_deployment,
                    instructions=spec.instructions,
                    tools=[tool],
                ),
                description=spec.description,
            )
            summary.append({
                "role": spec.role,
                "name": agent.name,
                "id": agent.id,
                "version": agent.version,
                "index": azure_index,
            })

    print("\nFoundry agents ready:")
    for row in summary:
        print(f"  {row['name']:32s} v{row['version']:<3} index={row['index']}")
    print("\nOpen the Foundry portal -> Agents -> select an agent -> Playground to chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
