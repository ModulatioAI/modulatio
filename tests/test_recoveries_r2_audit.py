# SPDX-License-Identifier: Apache-2.0
"""R2 full-debug audit regressions for the recovery feed.

Covers two LOW findings:
  * recoveries.py:361 — unconsumed_recoveries hard cap of 30 starved a genuinely
    recurring technique below the cluster floor.
  * recoveries.py:285 — write-time head-truncation flipped a real tail-only fix to an
    unclassified singleton that never clusters.
"""

from modulatio import recoveries


def _rec(monkeypatch, tmp_path, project, **kw):
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path / pc)
    base = dict(
        kind="qc_authored", artifact_kind="python_code", defect_type="substantive",
        task_id="T", defects="d", before="a", after="b", qc_rationale="r",
    )
    base.update(kw)
    return recoveries.record_recovery(project, **base)


# ── R2 #1: cap must not starve a recurring technique below the floor ───────────


def test_unconsumed_defaults_to_full_feed(tmp_path, monkeypatch):
    """No explicit limit → the FULL unconsumed feed (not a 30-row slice)."""
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path / pc)
    for i in range(40):
        _rec(monkeypatch, tmp_path, "P", task_id=f"T{i}",
             before=f"x{i}", after=f"y{i}")
    assert len(recoveries.unconsumed_recoveries("P")) == 40
    # an explicit positive limit still caps (preview callers)
    assert len(recoveries.unconsumed_recoveries("P", limit=5)) == 5


def test_recurring_technique_not_starved_by_diverse_newer_recoveries(tmp_path, monkeypatch):
    """A genuine 3-member recurring cluster that is OLDER than 30 diverse one-offs must
    still reach the floor. Before the fix, the default 30-row cap dropped the oldest
    matching members so the cluster fell below floor."""
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path / pc)
    # 3 same-signature recoveries (oldest), then 40 diverse one-offs (newer).
    same_kwargs = dict(
        artifact_kind="python_code",
        before="def f(x):\n    return x\n",
        after="def f(x):\n    if x is None:\n        return 0\n    return x\n",
        qc_rationale="missing null guard on input",
    )
    for i in range(3):
        _rec(monkeypatch, tmp_path, "P", task_id=f"S{i}",
             timestamp=f"2026-06-13T00:00:0{i}+00:00", **same_kwargs)
    for i in range(40):
        # distinct rationale + change → distinct signature each (one-offs)
        _rec(monkeypatch, tmp_path, "P", task_id=f"N{i}",
             timestamp=f"2026-06-13T10:{i:02d}:00+00:00",
             artifact_kind="python_code",
             before=f"a{i}\n", after=f"a{i}\nb{i}\n",
             qc_rationale=f"distinct one off rationale number {i}")

    recs = recoveries.unconsumed_recoveries("P")
    clusters = recoveries.cluster_recoveries(recs, floor=3)
    assert any(len(c) == 3 for c in clusters), (
        "the recurring 3-member technique was starved out of the window"
    )


# ── R2 #2: tail-only fix on a large artifact must still get a real shape ───────


def test_tail_fix_on_large_artifact_clusters(tmp_path, monkeypatch):
    """A real fix in the TAIL of a >MAX_RECOVERY_EXCERPT_CHARS artifact must produce a
    classified change-shape (not an unclassified singleton), so three such recoveries
    cluster. Before the fix the head-truncated before/after were identical → None →
    unclassified:<id> → permanent singleton."""
    monkeypatch.setattr(recoveries, "project_dir", lambda pc: tmp_path / pc)
    head = "common line\n" * 400  # well past MAX_RECOVERY_EXCERPT_CHARS (2000 chars)
    assert len(head) > recoveries.MAX_RECOVERY_EXCERPT_CHARS
    before = head + "def f(x):\n    return x\n"
    after = head + "def f(x):\n    if x is None:\n        return 0\n    return x\n"

    recs = [
        _rec(monkeypatch, tmp_path, "P", task_id=f"L{i}",
             artifact_kind="python_code",
             before=before, after=after,
             qc_rationale="missing null guard on input")
        for i in range(3)
    ]
    # none degraded to an unclassified singleton
    for r in recs:
        assert not r.signature.split("|")[-1].startswith("unclassified:"), r.signature
    # all three share one signature → one cluster of 3
    clusters = recoveries.cluster_recoveries(recs, floor=3)
    assert any(len(c) == 3 for c in clusters)


def test_delta_window_strips_common_prefix_and_suffix():
    """The fingerprint window isolates the changed region; identical strings stay a
    no-delta (so change_shape still fails open to a singleton)."""
    b = "AAAA\nMID_OLD\nZZZZ\n"
    a = "AAAA\nMID_NEW\nZZZZ\n"
    bw, aw = recoveries._delta_window(b, a)
    # char-level common prefix ("AAAA\nMID_") and suffix ("\nZZZZ\n") are stripped,
    # leaving only the genuinely-changed residue.
    assert "AAAA" not in bw and "ZZZZ" not in bw
    assert bw == "OLD" and aw == "NEW"
    # truly identical → unchanged, and change_shape returns None (no-delta)
    assert recoveries._delta_window("same", "same") == ("same", "same")
    assert recoveries.change_shape("same", "same", "python_code") is None


def test_head_truncated_tail_fix_would_have_collapsed():
    """Guard the premise: the OLD behaviour (shape from head-truncated excerpts) loses
    the tail delta — proving the fix is load-bearing."""
    head = "x" * (recoveries.MAX_RECOVERY_EXCERPT_CHARS + 50)
    before = head + "old"
    after = head + "new"
    # head-truncated copies are identical → change_shape sees no delta
    bt = before[: recoveries.MAX_RECOVERY_EXCERPT_CHARS]
    at = after[: recoveries.MAX_RECOVERY_EXCERPT_CHARS]
    assert bt == at
    assert recoveries.change_shape(bt, at, "python_code") is None
    # but the delta-windowed full strings DO yield a shape
    bw, aw = recoveries._delta_window(before, after)
    assert recoveries.change_shape(bw, aw, "python_code") is not None
