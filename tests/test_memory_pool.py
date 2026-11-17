"""Pool merge: a stale snapshot never loses newer data (RFC-0021, #3795)."""

import json

from memory import pool, retention, scratchpad

OLD = "2026-11-01T00:00:00+00:00"
NEW = "2026-11-16T00:00:00+00:00"


def _entry(n, last, bad=0, ab=0, **extra):
    return {"text": "boom", "n": n, "bad": bad, "ab": ab, "last": last, **extra}


def _scratch(path, entry, kind="error_fingerprint", key="k"):
    path.mkdir(parents=True, exist_ok=True)
    scratchpad.write(path, {kind: {key: entry}})


def test_stale_snapshot_does_not_lower_a_count(tmp_path):
    pool_dir, snap = tmp_path / "pool", tmp_path / "snap"
    _scratch(pool_dir, _entry(7, NEW))
    _scratch(snap, _entry(3, OLD))
    pool.merge_into(snap, pool_dir)
    assert scratchpad.read(pool_dir)["error_fingerprint"]["k"]["n"] == 7


def test_trust_and_abandon_marks_from_either_side_survive(tmp_path):
    pool_dir, snap = tmp_path / "pool", tmp_path / "snap"
    _scratch(pool_dir, _entry(7, NEW, bad=0, ab=2, promoted=True, body="b"))
    _scratch(snap, _entry(6, OLD, bad=1))
    pool.merge_into(snap, pool_dir)
    merged = scratchpad.read(pool_dir)["error_fingerprint"]["k"]
    assert (merged["n"], merged["bad"], merged["ab"], merged["promoted"]) == (
        7,
        1,
        2,
        True,
    )


def test_new_keys_are_added_and_dead_ends_stay_dead_ends(tmp_path):
    pool_dir, snap = tmp_path / "pool", tmp_path / "snap"
    _scratch(pool_dir, _entry(1, OLD), kind="dead_end", key="a")
    _scratch(snap, _entry(2, NEW), kind="dead_end", key="b")
    pool.merge_into(snap, pool_dir)
    assert set(scratchpad.read(pool_dir)["dead_end"]) == {"a", "b"}


def test_confirmed_keeps_the_later_stamp_and_gotchas_are_unioned(tmp_path):
    pool_dir, snap = tmp_path / "pool", tmp_path / "snap"
    pool_dir.mkdir()
    snap.mkdir()
    (pool_dir / "confirmed.json").write_text(json.dumps({"a": NEW, "b": OLD}))
    (snap / "confirmed.json").write_text(json.dumps({"a": OLD, "b": NEW, "c": OLD}))
    (pool_dir / "gotchas.md").write_text("# G\n\n## [2026-11-16 10:00]\nfirst\n")
    (snap / "gotchas.md").write_text(
        "# G\n\n## [2026-11-01 10:00]\nold\n\n## [2026-11-16 10:00]\nfirst\n"
    )
    pool.merge_into(snap, pool_dir)
    assert json.loads((pool_dir / "confirmed.json").read_text()) == {
        "a": NEW,
        "b": NEW,
        "c": OLD,
    }
    bodies = [
        b for _, b, _ in retention.split_gotchas((pool_dir / "gotchas.md").read_text())
    ]
    assert sorted(bodies) == ["first", "old"]


def test_other_files_are_copied_when_missing_or_newer(tmp_path):
    pool_dir, snap = tmp_path / "pool", tmp_path / "snap"
    (snap / "session_insights").mkdir(parents=True)
    (snap / "session_insights" / "session_001.json").write_text("{}")
    pool.merge_into(snap, pool_dir)
    assert (pool_dir / "session_insights" / "session_001.json").exists()


def test_mutation_wholesale_copy_loses_the_newer_count(tmp_path):
    import shutil

    pool_dir, snap = tmp_path / "pool", tmp_path / "snap"
    _scratch(pool_dir, _entry(7, NEW))
    _scratch(snap, _entry(3, OLD))
    shutil.copytree(snap, pool_dir, dirs_exist_ok=True)  # what the old sync did
    assert scratchpad.read(pool_dir)["error_fingerprint"]["k"]["n"] == 3
