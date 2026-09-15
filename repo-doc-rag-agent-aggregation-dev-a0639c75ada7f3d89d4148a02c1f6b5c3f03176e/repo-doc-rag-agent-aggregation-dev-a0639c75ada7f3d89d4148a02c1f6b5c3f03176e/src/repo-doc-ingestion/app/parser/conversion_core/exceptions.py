from typing import Optional


class AppError(Exception):
    """Base application exception with normalized fields for reporting."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        retryable: bool = False,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable
        self.cause = cause


class ConfigError(AppError):
    def __init__(self, message: str, *, stage: str = "config", cause: Optional[Exception] = None) -> None:
        super().__init__(message, stage=stage, retryable=False, cause=cause)


class ParseError(AppError):
    def __init__(self, message: str, *, stage: str = "parse", cause: Optional[Exception] = None) -> None:
        super().__init__(message, stage=stage, retryable=False, cause=cause)


class SummaryError(AppError):
    def __init__(
        self,
        message: str,
        *,
        stage: str = "summary",
        retryable: bool = True,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(message, stage=stage, retryable=retryable, cause=cause)


class TimeoutError(AppError):
    def __init__(self, message: str, *, stage: str = "timeout", cause: Optional[Exception] = None) -> None:
        super().__init__(message, stage=stage, retryable=True, cause=cause)


class DependencyError(ConfigError):
    def __init__(self, message: str, *, stage: str = "dependency", cause: Optional[Exception] = None) -> None:
        super().__init__(message, stage=stage, cause=cause)
