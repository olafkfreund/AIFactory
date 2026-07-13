"""
Base Module for Agent System
=============================

Shared imports, types, and constants used across agent modules.
"""

import logging
import os
import re

# Canonical fleet secret-redaction layer (vendored byte-for-byte from the Factory
# hub; epic Factory#154, issue Factory#161). We layer the AIFactory-specific
# redactions below ON TOP of this superset so this function gains the hub's
# coverage (GitHub PATs, AWS keys, Slack tokens, PEM private keys, URL userinfo,
# Authorization/PRIVATE-TOKEN header values) without losing any prior behaviour.
from factory_common.secrets import redact as redact_fleet_secrets

# Configure logging
logger = logging.getLogger(__name__)

# Configuration constants
AUTO_CONTINUE_DELAY_SECONDS = 3
HUMAN_INTERVENTION_FILE = "PAUSE"

# Retry configuration for subtask execution
MAX_SUBTASK_RETRIES = 5  # Maximum attempts before marking subtask as stuck

# Retry configuration for 400 tool concurrency errors
MAX_CONCURRENCY_RETRIES = 5  # Maximum number of retries for tool concurrency errors
INITIAL_RETRY_DELAY_SECONDS = (
    2  # Initial retry delay (doubles each retry: 2s, 4s, 8s, 16s, 32s)
)
MAX_RETRY_DELAY_SECONDS = 32  # Cap retry delay at 32 seconds

# Pause file constants for intelligent error recovery
# These files signal pause/resume between frontend and backend
RATE_LIMIT_PAUSE_FILE = "RATE_LIMIT_PAUSE"  # Created when rate limited
AUTH_FAILURE_PAUSE_FILE = "AUTH_PAUSE"  # Created when auth fails
RESUME_FILE = "RESUME"  # Created by frontend to signal resume

# Maximum time to wait for a rate-limit reset before failing the task. Kept
# BELOW the k8s build deadline (job_dispatch.py deadline_seconds, default 3600s)
# so a rate-limit wait can never silently consume the whole deadline and leave an
# empty patch (#816 fix #3) — a wait that would outlast the build fails fast so
# the task can retry. NOTE: the live #272 auto-resume path is separately bounded
# by RateLimitResumePolicy.max_total_wait_seconds (1800s, error_utils.py); this
# constant guards the legacy pause-file wait. Override via the env if the
# deadline is raised.
MAX_RATE_LIMIT_WAIT_SECONDS = int(
    os.environ.get("AIFACTORY_MAX_RATE_LIMIT_WAIT_SECONDS", "3300")
)

# Wait intervals for pause/resume checking
RATE_LIMIT_CHECK_INTERVAL_SECONDS = (
    30  # Check for RESUME file every 30 seconds during rate limit wait
)
AUTH_RESUME_CHECK_INTERVAL_SECONDS = 10  # Check for re-authentication every 10 seconds
AUTH_RESUME_MAX_WAIT_SECONDS = 86400  # Maximum wait for re-authentication (24 hours)


def sanitize_error_message(error_message: str, max_length: int = 500) -> str:
    """
    Sanitize error messages to remove potentially sensitive information.

    Redacts:
    - API keys (sk-..., key-...)
    - Bearer tokens
    - Token/secret values

    Args:
        error_message: The raw error message to sanitize
        max_length: Maximum length to truncate to (default 500)

    Returns:
        Sanitized and truncated error message
    """
    if not error_message:
        return ""

    # Step 1: apply the canonical fleet redaction first (the superset pattern
    # table). This catches credential shapes the AIFactory-specific rules below
    # never covered - GitHub/GitLab PATs, AWS access keys, Slack tokens, PEM
    # private keys, URL userinfo, and Authorization:/PRIVATE-TOKEN: header values
    # - replacing them with the fleet ***REDACTED*** placeholder.
    sanitized = redact_fleet_secrets(error_message)

    # Step 2: layer the AIFactory-specific redactions (preserved verbatim, with
    # their original placeholder strings) for the shapes the fleet table does not
    # target: provider sk-/key- API keys and bare token=/secret=/Bearer values.
    # Pattern: sk-... (OpenAI/Anthropic keys like sk-ant-api03-...)
    sanitized = re.sub(r"\bsk-[a-zA-Z0-9._\-]{20,}\b", "[REDACTED_API_KEY]", sanitized)

    # Pattern: key-... (generic API keys)
    sanitized = re.sub(r"\bkey-[a-zA-Z0-9._\-]{20,}\b", "[REDACTED_API_KEY]", sanitized)

    # Pattern: Bearer ... (bearer tokens)
    sanitized = re.sub(
        r"\bBearer\s+[a-zA-Z0-9._\-]{20,}\b", "Bearer [REDACTED_TOKEN]", sanitized
    )

    # Pattern: token= or token: followed by long strings
    sanitized = re.sub(
        r"(token[=:]\s*)[a-zA-Z0-9._\-]{20,}\b",
        r"\1[REDACTED_TOKEN]",
        sanitized,
        flags=re.IGNORECASE,
    )

    # Pattern: secret= or secret: followed by strings
    sanitized = re.sub(
        r"(secret[=:]\s*)[a-zA-Z0-9._\-]{20,}\b",
        r"\1[REDACTED_SECRET]",
        sanitized,
        flags=re.IGNORECASE,
    )

    # Truncate to max length
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."

    return sanitized
