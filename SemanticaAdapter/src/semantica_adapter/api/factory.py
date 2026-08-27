"""Convenience bootstrap for local Semantica-backed prototypes."""

from pathlib import Path

from semantica_adapter.adapters.memory import (
    MemoryAgentProfileRepository,
    MemoryApprovalWorkflow,
)
from semantica_adapter.services.governance import AgentGovernanceService


def create_local_semantica_service(
    *,
    authorized_actors: set[tuple[str, str]],
    provenance_storage_path: Path | None = None,
) -> AgentGovernanceService:
    """Build a local prototype service.

    Production deployments should inject the bank's durable profile repository
    and identity-aware approval workflow into ``AgentGovernanceService``.
    """

    try:
        from semantica_adapter.adapters.semantica import SemanticaBackend, SemanticaConfig
    except ImportError as error:
        raise RuntimeError(
            "The local Semantica backend is optional; install "
            "'semantica-adapter[semantica]' or 'semantica-adapter[server]'."
        ) from error

    backend = SemanticaBackend(
        SemanticaConfig(provenance_storage_path=provenance_storage_path)
    )
    return AgentGovernanceService(
        backend,
        MemoryAgentProfileRepository(),
        MemoryApprovalWorkflow(authorized_actors),
    )
