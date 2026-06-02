"""
QA Validation Package
=====================

Modular QA validation system with:
- Acceptance criteria validation
- Issue tracking and reporting
- Recurring issue detection
- QA reviewer and fixer agents
- Main orchestration loop

Usage:
    from qa import run_qa_validation_loop, should_run_qa, is_qa_approved

Module structure:
    - loop.py: Main QA orchestration loop
    - reviewer.py: QA reviewer agent session
    - fixer.py: QA fixer agent session
    - report.py: Issue tracking, reporting, escalation
    - criteria.py: Acceptance criteria and status management
"""

# Configuration constants
# Criteria & status
from .criteria import (
    get_qa_iteration_count,
    get_qa_signoff_status,
    is_fixes_applied,
    is_qa_approved,
    is_qa_rejected,
    load_implementation_plan,
    print_qa_status,
    save_implementation_plan,
    should_run_fixes,
    should_run_qa,
)
from .fixer import (
    load_qa_fixer_prompt,
    run_qa_fixer_session,
)

# Main loop
from .loop import MAX_QA_ITERATIONS, run_qa_validation_loop

# Report & tracking
from .report import (
    ISSUE_SIMILARITY_THRESHOLD,
    RECURRING_ISSUE_THRESHOLD,
    _issue_similarity,
    # Private functions exposed for testing
    _normalize_issue_key,
    check_test_discovery,
    create_manual_test_plan,
    escalate_to_human,
    get_iteration_history,
    get_recurring_issue_summary,
    has_recurring_issues,
    is_no_test_project,
    record_iteration,
)

# Review-cycle obligation tracking (#260)
from .review_cycle import (
    CycleState,
    EngagementProof,
    InvalidTransitionError,
    ReviewCycle,
    ReviewCycleError,
    StaleCycleError,
    detect_untouched_review,
    load_cycle,
    record_redrive,
    record_started,
    redrive_untouched_review,
    request_review,
    resolve_review,
)

# Review re-drive: nudge + human escalation for stuck reviews (#260)
from .review_redrive import (
    REVIEWER_RECIPIENT,
    process_untouched_review,
)

# Agent sessions
from .reviewer import run_qa_agent_session

# Public API
__all__ = [
    # Configuration
    "MAX_QA_ITERATIONS",
    "RECURRING_ISSUE_THRESHOLD",
    "ISSUE_SIMILARITY_THRESHOLD",
    # Main loop
    "run_qa_validation_loop",
    # Criteria & status
    "load_implementation_plan",
    "save_implementation_plan",
    "get_qa_signoff_status",
    "is_qa_approved",
    "is_qa_rejected",
    "is_fixes_applied",
    "get_qa_iteration_count",
    "should_run_qa",
    "should_run_fixes",
    "print_qa_status",
    # Report & tracking
    "get_iteration_history",
    "record_iteration",
    "has_recurring_issues",
    "get_recurring_issue_summary",
    "escalate_to_human",
    "create_manual_test_plan",
    "check_test_discovery",
    "is_no_test_project",
    "_normalize_issue_key",
    "_issue_similarity",
    # Review-cycle obligation tracking (#260)
    "CycleState",
    "EngagementProof",
    "ReviewCycle",
    "ReviewCycleError",
    "InvalidTransitionError",
    "StaleCycleError",
    "request_review",
    "record_started",
    "resolve_review",
    "record_redrive",
    "load_cycle",
    "detect_untouched_review",
    "redrive_untouched_review",
    # Review re-drive (#260)
    "REVIEWER_RECIPIENT",
    "process_untouched_review",
    # Agent sessions
    "run_qa_agent_session",
    "load_qa_fixer_prompt",
    "run_qa_fixer_session",
]
