"""Stable Agent-governance interface backed by pluggable providers."""

__version__ = "0.1.0"
SEMANTICA_COMPAT_VERSION = "0.6.6"

from .api import AgentGovernanceService, create_local_semantica_service
from .domain.errors import (
    ApprovalRequiredError,
    AuditIntegrityError,
    BackendError,
    ConfigurationError,
    EvidenceError,
    SemanticaAdapterError,
    UnsupportedCapabilityError,
    ValidationError,
)
from .domain.models import (
    AgentProfile,
    ApprovalRecord,
    AuditExport,
    AuditRequest,
    AuditSession,
    AuditStatus,
    AuditTrace,
    DecisionRecord,
    EvidenceRef,
    PolicyExceptionRecord,
    RuleEvaluation,
)
from .services.integrity import (
    AuditPackage,
    IntegrityResult,
    verify_export_package,
)

__all__ = [
    "AgentGovernanceService",
    "AgentProfile",
    "ApprovalRecord",
    "ApprovalRequiredError",
    "AuditExport",
    "AuditIntegrityError",
    "AuditPackage",
    "AuditRequest",
    "AuditSession",
    "AuditStatus",
    "AuditTrace",
    "BackendError",
    "ConfigurationError",
    "DecisionRecord",
    "EvidenceError",
    "EvidenceRef",
    "IntegrityResult",
    "PolicyExceptionRecord",
    "RuleEvaluation",
    "SEMANTICA_COMPAT_VERSION",
    "SemanticaAdapterError",
    "UnsupportedCapabilityError",
    "ValidationError",
    "__version__",
    "create_local_semantica_service",
    "verify_export_package",
]
