"""Errors shared by configuration, contracts and future model adapters."""


class ConfigurationError(ValueError):
    """Invalid local configuration or missing model configuration."""


class ContractError(ValueError):
    """An object violates the internal message or serialization contract."""


class ModelError(Exception):
    """Provider error metadata; retry policy belongs to the runtime.

    Adapters must supply a credential-free message and request identifier.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        provider_request_id: str | None = None,
        retry_after: float | None = None,
        error_type: str | None = None,
        context_too_long: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.provider_request_id = provider_request_id
        self.retry_after = retry_after
        self.error_type = error_type
        self.context_too_long = context_too_long



class StorageError(Exception):
    """Critical persistence failure: stop issuing side effects."""
