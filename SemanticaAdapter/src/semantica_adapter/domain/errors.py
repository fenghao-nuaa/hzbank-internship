"""Stable exceptions exposed to company agents."""


class SemanticaAdapterError(RuntimeError):
    """Base class for public adapter failures."""


class ConfigurationError(SemanticaAdapterError):
    """The selected backend cannot be configured safely."""


class UnsupportedCapabilityError(SemanticaAdapterError):
    """The selected backend does not implement a requested capability."""


class ValidationError(SemanticaAdapterError):
    """Input, ontology, or rule validation failed."""


class EvidenceError(SemanticaAdapterError):
    """Evidence is absent, inaccessible, or has an invalid digest."""


class BackendError(SemanticaAdapterError):
    """A backend operation failed."""


class AuditIntegrityError(SemanticaAdapterError):
    """An audit package or audit chain failed integrity verification."""


class ApprovalRequiredError(SemanticaAdapterError):
    """A caller attempted to bypass a required approval."""
