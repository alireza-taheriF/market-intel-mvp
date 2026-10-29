"""Typed errors for external services.

Every failure of an external dependency (search, web, LLM, SMTP) is converted
into one of these so the pipeline and the UI can react in a controlled way
and show a human-readable message instead of a stack trace.
"""
from __future__ import annotations


class ExternalServiceError(Exception):
    """Base class for any failure of an external dependency."""

    service = "external service"
    hint = "Please try again later."

    def __init__(self, message: str, *, service: str | None = None):
        super().__init__(message)
        if service:
            self.service = service

    @property
    def user_message(self) -> str:
        return f"{self.service}: {self} — {self.hint}"


class NetworkError(ExternalServiceError):
    hint = "Check the internet connection / proxy settings (HTTPS_PROXY)."


class ServiceTimeoutError(ExternalServiceError):
    hint = "The service did not answer in time. Retry, or increase the timeout."


class InvalidResponseError(ExternalServiceError):
    hint = "The service returned an empty or malformed answer. Retry the analysis."


class SearchError(ExternalServiceError):
    service = "Web search"


class LLMUnavailableError(ExternalServiceError):
    service = "AI model"
    hint = "Check the API key in .env (OPENAI_API_KEY / ANTHROPIC_API_KEY), the model name, network access, or retry later."


class EmailError(ExternalServiceError):
    service = "Email (SMTP)"
    hint = "Check the SMTP_* settings in .env and the network connection."


class RecipientNotAllowedError(EmailError):
    hint = "Test emails may only be sent to the single whitelisted address."
