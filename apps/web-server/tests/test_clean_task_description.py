"""Tests for the cockpit Overview defensive guard.

``_clean_task_description`` is a belt-and-suspenders product guard so the build
cockpit task Overview can never again render a stringified Python/JSON dict as a
wall of text (see aifactory-demo#206, AIFactory#612). Clean prose must pass
through untouched; stringified dicts must collapse to a short readable string.
"""

from collections.abc import Callable
from typing import cast

from server.routes.tasks import _clean_task_description


def test_clean_markdown_passes_through_unchanged() -> None:
    desc = (
        "# Build the widget\n\n"
        "Implement the **widget** service with a `/health` endpoint.\n\n"
        "- item one\n- item two\n"
    )
    assert _clean_task_description(desc) == desc


def test_plain_prose_with_stray_brace_untouched() -> None:
    # A lone brace in prose must NOT trip the mapping heuristic.
    desc = "Use the format {placeholder} when rendering the template."
    assert _clean_task_description(desc) == desc


def test_full_stringified_epic_plan_dict_collapses_to_summary() -> None:
    # The observed shape: a whole EpicPlan dict str()-ed into the description.
    epic_plan = (
        "{'plan_id': 'plan-abc123', "
        "'title': 'Taskboard service', "
        "'description': 'Build a taskboard API with CRUD and auth.', "
        "'effort_estimate': 'L', "
        "'children': [{'id': 1, 'title': 'schema'}, {'id': 2, 'title': 'api'}]}"
    )
    result = _clean_task_description(epic_plan)

    # Prefers the human-readable description field.
    assert result == "Build a taskboard API with CRUD and auth."
    # Never leaks dict guts.
    assert "children" not in result
    assert "effort_estimate" not in result
    assert "{" not in result and "}" not in result


def test_stringified_json_dict_collapses_to_summary() -> None:
    payload = (
        '{"plan_id": "plan-xyz", "summary": "Add rate limiting to the gateway", '
        '"children": [1, 2, 3], "effort_estimate": "M"}'
    )
    result = _clean_task_description(payload)
    assert result == "Add rate limiting to the gateway"
    assert "children" not in result
    assert "{" not in result


def test_dict_without_readable_field_falls_back_to_title_then_plan_id() -> None:
    only_title = "{'title': 'Migrate to FastAPI 0.136', 'children': [1, 2]}"
    assert _clean_task_description(only_title) == "Migrate to FastAPI 0.136"

    only_plan_id = "{'plan_id': 'plan-001', 'children': [1, 2], 'effort_estimate': 'S'}"
    assert _clean_task_description(only_plan_id) == "plan-001"


def test_embedded_correlation_epic_dict_collapses_to_clean_reference() -> None:
    desc = (
        "Implement the auth flow.\n\n"
        "Correlation epic #{'plan_id': 'plan-7', 'children': [1, 2], "
        "'effort_estimate': 'L', 'title': 'Auth epic'}\n\n"
        "See the linked PFactory plan."
    )
    result = _clean_task_description(desc)

    assert "Correlation epic #plan-7" in result
    assert "Implement the auth flow." in result
    assert "See the linked PFactory plan." in result
    # No dict guts survive the collapse.
    assert "children" not in result
    assert "effort_estimate" not in result
    assert "{" not in result and "}" not in result


def test_embedded_correlation_epic_without_plan_id_collapses_bare() -> None:
    desc = "Do the thing. Correlation epic #{'foo': 'bar', 'baz': [1, 2]}"
    result = _clean_task_description(desc)
    assert "Correlation epic" in result
    assert "{" not in result
    assert "#{" not in result


def test_malformed_dict_never_raises_and_strips_braces() -> None:
    # Looks like a mapping (starts {, ends }, has ':) but is not parseable.
    malformed = "{'plan_id': 'plan-9', 'children': [1, 2, <not valid>]}"
    result = _clean_task_description(malformed)  # must not raise
    assert "{" not in result and "}" not in result
    assert "\n" not in result


def test_malformed_long_dict_is_truncated_single_line() -> None:
    malformed = "{'k': '" + ("x" * 500) + "', oops}"
    result = _clean_task_description(malformed)  # must not raise
    assert "\n" not in result
    assert len(result) <= 201  # ~200 chars + ellipsis


def test_non_string_input_returned_unchanged() -> None:
    # Defensive: the helper must tolerate a non-str description without raising
    # and return it unchanged. Typed via a deliberately wide annotation so the
    # static type checker does not narrow away the non-str runtime behaviour.
    fn = cast("Callable[[object], object]", _clean_task_description)
    assert fn(None) is None
    assert fn(123) == 123


def test_empty_and_whitespace_passthrough() -> None:
    assert _clean_task_description("") == ""
    assert _clean_task_description("   ") == "   "
