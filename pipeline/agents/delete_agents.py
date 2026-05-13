"""Delete every FinSight US Foundry agent provisioned by create_agents.py.

Useful while iterating on instructions or tools.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient

from common import env
from agents.specs import AGENT_SPECS


def main() -> int:
    project_endpoint = env("AZURE_AI_PROJECT_ENDPOINT", required=True)
    prefix = env("FOUNDRY_AGENT_PREFIX", "finsight-us")

    deleted = []
    with (
        AzureCliCredential() as credential,
        AIProjectClient(endpoint=project_endpoint, credential=credential) as project_client,
    ):
        for spec in AGENT_SPECS:
            agent_name = f"{prefix}-{spec.role}"
            try:
                project_client.agents.delete(agent_name)
                deleted.append(agent_name)
                print(f"deleted {agent_name}")
            except Exception as ex:  # noqa: BLE001
                if "404" in str(ex) or "ResourceNotFound" in str(ex):
                    print(f"absent  {agent_name}")
                else:
                    print(f"WARN    {agent_name}: {ex}", file=sys.stderr)
    print(f"\nDeleted {len(deleted)} agents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
