from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ExternalRequestError(Exception):
    message: str
    code: str = "EXTERNAL_UNAVAILABLE"
    domain: str = "external"
    retryable: bool = True
    recovery_action: str = "retry_later"
    retry_after: float | None = None
    provider: str | None = None
    credential_id: str | None = None
    external_operation_id: str | None = None

    def __str__(self) -> str:
        return self.message


def classify_external_error(exc: Exception) -> ExternalRequestError:
    if isinstance(exc, ExternalRequestError):
        return exc
    code = str(
        getattr(exc, "error_code", "") or getattr(exc, "code", "") or ""
    )
    value = str(exc).lower()
    if code in {"TASK_CANCELLED"}:
        return ExternalRequestError(str(exc), code, "cancelled", False, "none")
    if code in {"PROVIDER_CAPACITY_WAIT", "PROVIDER_CIRCUIT_OPEN"}:
        return ExternalRequestError(
            str(exc),
            code,
            "external",
            True,
            "backpressure",
            getattr(exc, "retry_after", None),
            getattr(exc, "provider", None),
            getattr(exc, "credential_id", None),
        )
    if code in {"INVALID_INPUT", "INVALID_PDF", "LICENSE_NOT_ALLOWED", "UNSAFE_PDF_URL"} or any(word in value for word in ("invalid", "unsupported", "license", "unsafe")):
        return ExternalRequestError(str(exc), code or "INVALID_INPUT", "input", False, "fix_input")
    if code in {"PROVIDER_AUTH_FAILED", "PROVIDER_FORBIDDEN"} or "401" in value or "403" in value:
        return ExternalRequestError(str(exc), code or "PROVIDER_AUTH_FAILED", "external", False, "replace_credential", provider=getattr(exc, "provider", None), credential_id=getattr(exc, "credential_id", None))
    if code in {
        "PROVIDER_RATE_LIMITED",
        "PDF_RATE_LIMITED",
        "UPSTREAM_RATE_LIMITED",
        "EXTERNAL_RATE_LIMITED",
    } or "429" in value or "rate limit" in value:
        normalized_code = (
            "PROVIDER_RATE_LIMITED"
            if code == "PROVIDER_RATE_LIMITED"
            else "EXTERNAL_RATE_LIMITED"
        )
        return ExternalRequestError(
            str(exc),
            normalized_code,
            "external",
            True,
            "cooldown_and_retry",
            getattr(exc, "retry_after", None),
            getattr(exc, "provider", None),
            getattr(exc, "credential_id", None),
        )
    if code in {"RESOURCE_NOT_FOUND", "PDF_HOST_UNRESOLVED"} or "not found" in value:
        return ExternalRequestError(str(exc), code or "EXTERNAL_NOT_FOUND", "external", False, "reresolve_source")
    if code in {"DATA_INTEGRITY_BLOCKED"} or any(
        marker in value for marker in ("foreign key", "cannot contain nul")
    ):
        return ExternalRequestError(str(exc), code or "DATA_INTEGRITY_BLOCKED", "data", False, "repair_data")
    if code in {"PROVIDER_TIMEOUT", "PROVIDER_UNAVAILABLE", "QUEUE_UNAVAILABLE"} or any(word in value for word in ("timeout", "timed out", "connection", "http 5")):
        timeout = "timeout" in value or "timed out" in value
        return ExternalRequestError(str(exc), code or ("PROVIDER_TIMEOUT" if timeout else "PROVIDER_UNAVAILABLE"), "external" if "provider" in code.lower() or timeout or "connection" in value else "platform", True, "retry_later", getattr(exc, "retry_after", None), getattr(exc, "provider", None), getattr(exc, "credential_id", None))
    return ExternalRequestError(
        str(exc),
        code or "TASK_EXECUTION_FAILED",
        "platform",
        bool(getattr(exc, "retryable", False)),
        "retry_later" if getattr(exc, "retryable", False) else "inspect",
        getattr(exc, "retry_after", None),
        getattr(exc, "provider", None),
        getattr(exc, "credential_id", None),
    )
