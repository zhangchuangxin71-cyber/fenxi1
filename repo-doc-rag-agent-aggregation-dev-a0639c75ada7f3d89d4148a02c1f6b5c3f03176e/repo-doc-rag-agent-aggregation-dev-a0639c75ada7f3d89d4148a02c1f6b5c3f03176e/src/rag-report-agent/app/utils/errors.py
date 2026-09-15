from __future__ import annotations


class FrontendServiceError(Exception):
    """Exception that should be surfaced to the frontend as a stable error event."""

    def __init__(
        self,
        *,
        status_code: int,
        error_type: str,
        public_message: str,
        internal_message: str = "",
    ) -> None:
        super().__init__(internal_message or public_message)
        self.status_code = int(status_code)
        self.error_type = error_type
        self.public_message = public_message
        self.internal_message = internal_message or public_message
