#!/usr/bin/env python3
"""
Tests for solo + parallel wave routing (issue #389)
===================================================

Solo mode collapses planning and implementation into one session, which
bypasses the parallel wave dispatch. ``solo_session_plans_inline`` decides
whether the solo first session implements inline (single session) or defers to
a planner-first flow so the wave dispatch can run independent subtasks
concurrently.
"""

import pytest
from agents.coder import solo_session_plans_inline


class TestSoloSessionPlansInline:
    def test_solo_without_parallel_plans_inline(self):
        # Solo alone keeps its single-session collapse (no regression).
        assert solo_session_plans_inline(solo=True, parallel=False) is True

    def test_solo_with_parallel_defers_to_planner(self):
        # Under --parallel, author the plan first so waves can engage (#389).
        assert solo_session_plans_inline(solo=True, parallel=True) is False

    def test_non_solo_never_plans_inline(self):
        # Non-solo always uses the dedicated planner session.
        assert solo_session_plans_inline(solo=False, parallel=False) is False
        assert solo_session_plans_inline(solo=False, parallel=True) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
