"""User-facing errors. Messages must never contain PII or local/Drive file paths."""

from __future__ import annotations


class KneeError(Exception):
    """An error whose message is safe to show to the client."""


class NotFound(KneeError):
    pass


class RedactionFailed(KneeError):
    def __init__(self) -> None:
        super().__init__(
            "redaction check failed: text that may identify the patient survived masking, "
            "so the image was withheld"
        )
