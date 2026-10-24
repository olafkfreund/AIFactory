"""RFC-0021 baseline script: pair counting and pooled-store loading (#389)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from memory_dup_baseline import count_pairs, lexical_vector, load_items  # noqa: E402


def test_count_pairs_exact_near_and_distinct():
    texts = [
        "Always close database connections in workers",
        "always  close database connections in WORKERS",  # exact after folding
        "Always close database connections in the workers",  # near
        "Playwright needs the node cli directly",
    ]
    exact, near = count_pairs(texts, [lexical_vector(t) for t in texts])
    assert exact == 1
    assert near == 2  # the near text pairs with both exact twins


def test_load_items_reads_gotchas_and_insights(tmp_path):
    (tmp_path / "gotchas.md").write_text(
        "# G\n\n- bullet gotcha number one\n- bullet gotcha number two\n\n"
        "## [2026-01-01]\nparagraph style gotcha text\n"
    )
    d = tmp_path / "session_insights"
    d.mkdir()
    (d / "session_001.json").write_text(
        json.dumps({"what_worked": ["something that worked fine"], "timestamp": "x"})
    )
    assert len(load_items(tmp_path)) == 4
