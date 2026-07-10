"""
GitHub Automation Error Types
=============================

Structured error types for GitHub automation with:
- Serializable error objects for IPC
- Stack trace preservation
- Error categorization for UI display
- Actionable error messages with retry hints
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    """Categories of errors for UI display and handling."""

    # Authentication/Permission errors
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    TOKEN_EXPIRED = "token_expired"
    INSUFFICIENT_SCOPE = "insufficient_scope"

    # Rate limiting errors
    RATE_LIMITED = "rate_limited"
    COST_EXCEEDED = "cost_exceeded"

    # Network/API errors
    NETWORK = "network"
    TIMEOUT = "timeout"
    API_ERROR = "api_error"
    SERVICE_UNAVAILABLE = "service_unavailable"

    # Validation errors
    VALIDATION = "validation"
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"

    # State errors
    INVALID_STATE = "invalid_state"
    CONFLICT = "conflict"
    ALREADY_EXISTS = "already_exists"

    # Internal errors
    INTERNAL = "internal"
    CONFIGURATION = "configuration"

    # Bot/Automation errors
    BOT_DETECTED = "bot_detected"
    CANCELLED = "cancelled"


class ErrorSeverity(str, Enum):
    """Severity levels for errors."""

    INFO = "info"  # Informational, not really an error
    WARNING = "warning"  # Something went wrong but recoverable
    ERROR = "error"  # Operation failed
    CRITICAL = "critical"  # System-level failure


@dataclass
class StructuredError:
    """
    Structured error object for IPC and UI display.

    This class provides:
    - Serialization for sending errors to frontend
    - Stack trace preservation
    - Actionable messages and retry hints
    - Error categorization
    """

    # Core error info
    message: str
    category: ErrorCategory
    severity: ErrorSeverity = ErrorSeverity.ERROR

    # Context
    code: str | None = None  # Machine-readable error code
    correlation_id: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Details
    details: dict[str, Any] = field(default_factory=dict)
    stack_trace: str | None = None

    # Recovery hints
    retryable: bool = False
    retry_after_seconds: int | None = None
    action_hint: str | None = None  # e.g., "Click retry to attempt again"
    help_url: str | None = None

    # Source info
    source: str | None = None  # e.g., "orchestrator.review_pr"
    pr_number: int | None = None
    issue_number: int | None = None
    repo: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "message": self.message,
            "category": self.category.value,
            "severity": self.severity.value,
            "code": self.code,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "details": self.details,
            "stack_trace": self.stack_trace,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "action_hint": self.action_hint,
            "help_url": self.help_url,
            "source": self.source,
            "pr_number": self.pr_number,
            "issue_number": self.issue_number,
            "repo": self.repo,
        }

    @classmethod
    def from_exception(
        cls,
        exc: Exception,
        category: ErrorCategory = ErrorCategory.INTERNAL,
        severity: ErrorSeverity = ErrorSeverity.ERROR,
        correlation_id: str | None = None,
        **kwargs,
    ) -> StructuredError:
        """Create a StructuredError from an exception."""
        return cls(
            message=str(exc),
            category=category,
            severity=severity,
            correlation_id=correlation_id,
            stack_trace=traceback.format_exc(),
            code=exc.__class__.__name__,
            **kwargs,
        )


# Custom Exception Classes with structured error support


class GitHubAutomationError(Exception):
    """Base exception for GitHub automation errors."""

    category: ErrorCategory = ErrorCategory.INTERNAL
    severity: ErrorSeverity = ErrorSeverity.ERROR
    retryable: bool = False
    action_hint: str | None = None

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        **kwargs,
    ):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.correlation_id = correlation_id
        self.extra = kwargs

    def to_structured_error(self) -> StructuredError:
        """Convert to StructuredError for IPC."""
        return StructuredError(
            message=self.message,
            category=self.category,
            severity=self.severity,
            code=self.__class__.__name__,
            correlation_id=self.correlation_id,
            details=self.details,
            stack_trace=traceback.format_exc(),
            retryable=self.retryable,
            action_hint=self.action_hint,
            **self.extra,
        )


class TimeoutError(GitHubAutomationError):
    """Operation timed out."""

    category = ErrorCategory.TIMEOUT
    retryable = True
    action_hint = "The operation took too long. Try again"


# Error handling utilities


def capture_error(
    exc: Exception,
    correlation_id: str | None = None,
    source: str | None = None,
    pr_number: int | None = None,
    issue_number: int | None = None,
    repo: str | None = None,
) -> StructuredError:
    """
    Capture any exception as a StructuredError.

    Handles both GitHubAutomationError subclasses and generic exceptions.
    """
    if isinstance(exc, GitHubAutomationError):
        error = exc.to_structured_error()
        error.source = source
        error.pr_number = pr_number
        error.issue_number = issue_number
        error.repo = repo
        if correlation_id:
            error.correlation_id = correlation_id
        return error

    # Map known exception types to categories
    category = ErrorCategory.INTERNAL
    retryable = False

    if isinstance(exc, TimeoutError):
        category = ErrorCategory.TIMEOUT
        retryable = True
    elif isinstance(exc, ConnectionError):
        category = ErrorCategory.NETWORK
        retryable = True
    elif isinstance(exc, PermissionError):
        category = ErrorCategory.PERMISSION
    elif isinstance(exc, FileNotFoundError):
        category = ErrorCategory.NOT_FOUND
    elif isinstance(exc, ValueError):
        category = ErrorCategory.VALIDATION

    return StructuredError.from_exception(
        exc,
        category=category,
        correlation_id=correlation_id,
        source=source,
        pr_number=pr_number,
        issue_number=issue_number,
        repo=repo,
        retryable=retryable,
    )


def format_error_for_ui(error: StructuredError) -> dict[str, Any]:
    """
    Format error for frontend UI display.

    Returns a simplified structure optimized for UI rendering.
    """
    return {
        "title": _get_error_title(error.category),
        "message": error.message,
        "severity": error.severity.value,
        "retryable": error.retryable,
        "retry_after": error.retry_after_seconds,
        "action": error.action_hint,
        "details": {
            "code": error.code,
            "correlation_id": error.correlation_id,
            "timestamp": error.timestamp,
            **error.details,
        },
        "expandable": {
            "stack_trace": error.stack_trace,
            "help_url": error.help_url,
        },
    }


def _get_error_title(category: ErrorCategory) -> str:
    """Get human-readable title for error category."""
    titles = {
        ErrorCategory.AUTHENTICATION: "Authentication Failed",
        ErrorCategory.PERMISSION: "Permission Denied",
        ErrorCategory.TOKEN_EXPIRED: "Token Expired",
        ErrorCategory.INSUFFICIENT_SCOPE: "Insufficient Permissions",
        ErrorCategory.RATE_LIMITED: "Rate Limited",
        ErrorCategory.COST_EXCEEDED: "Cost Limit Exceeded",
        ErrorCategory.NETWORK: "Network Error",
        ErrorCategory.TIMEOUT: "Operation Timed Out",
        ErrorCategory.API_ERROR: "GitHub API Error",
        ErrorCategory.SERVICE_UNAVAILABLE: "Service Unavailable",
        ErrorCategory.VALIDATION: "Validation Error",
        ErrorCategory.INVALID_INPUT: "Invalid Input",
        ErrorCategory.NOT_FOUND: "Not Found",
        ErrorCategory.INVALID_STATE: "Invalid State",
        ErrorCategory.CONFLICT: "Conflict Detected",
        ErrorCategory.ALREADY_EXISTS: "Already Exists",
        ErrorCategory.INTERNAL: "Internal Error",
        ErrorCategory.CONFIGURATION: "Configuration Error",
        ErrorCategory.BOT_DETECTED: "Bot Activity Detected",
        ErrorCategory.CANCELLED: "Operation Cancelled",
    }
    return titles.get(category, "Error")
