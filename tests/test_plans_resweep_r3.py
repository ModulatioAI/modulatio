"""Round-3 re-sweep regression tests for two 0.9.0 pre-ship findings on
plans.py. Each test FAILS against the pre-fix code and PASSES after.

Finding 1 (_plan_lock, MEDIUM/race):
    The re-entrancy key was computed as
    ``str(lock_path.resolve() if lock_path.exists() else lock_path)``.
    On the OUTER acquisition the lock file doesn't exist yet (touch()ed
    later in the same call) → key is the UNRESOLVED path. On a NESTED
    re-entrant acquisition the file now exists → key is the symlink-
    RESOLVED path. Under a symlinked plans-root the two keys diverge, so
    the nested call misses the depth>0 fast-path and re-grabs fcntl on
    the same fd → self-deadlock. Fix: compute the key unconditionally as
    ``str(lock_path.resolve())`` so both acquisitions agree.

Finding 2 (_coerce_cap, LOW/correctness):
    A negative cap (``max_tokens: -1``) was coerced as-is; ``over_cap()``
    returns a halt reason on the first usage record (``0 > -1``), so the
    plan halts immediately with a confusing "cap exceeded". Fix: a
    negative coerced cap degrades to ``None`` (unbounded), mirroring
    ``comptroller._parse_int``.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from modulatio import plans, vault


@pytest.fixture
def symlinked_vault(tmp_path: Path, monkeypatch):
    """Vault root reached through a symlinked component, so that
    ``Path.resolve()`` (symlink-following) differs from the literal path.
    This is the exact condition that made the outer/inner lock keys
    diverge in Finding 1."""
    real_root = tmp_path / "real_projects"
    real_root.mkdir()
    link_root = tmp_path / "linked_projects"
    link_root.symlink_to(real_root, target_is_directory=True)
    # Point the vault at the SYMLINK; resolve() will follow it to real_root.
    monkeypatch.setattr(vault, "VAULT_ROOT", link_root)
    vault.init_project("tst", "Test", "obj")
    return link_root


def _make_plan(project_code: str = "tst") -> "plans.PlanRecord":
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\n\n### Sub-objectives\n\n### Risks\n"
    )
    return plans.persist(
        body, project_code=project_code, agent_id="leader",
        source_message="please plan",
    )


# ── Finding 1: nested re-entrant lock under a symlinked root ────────────


def test_reentrant_plan_lock_under_symlinked_root_does_not_deadlock(
    symlinked_vault,
):
    record = _make_plan()

    # Run the nested acquisition on a worker thread with a hard timeout.
    # Pre-fix: the inner acquisition computes a RESOLVED key (file now
    # exists) that differs from the outer UNRESOLVED key, so it tries to
    # re-grab the non-recursive flock on the same fd and self-deadlocks —
    # the thread never finishes within the join timeout.
    done = threading.Event()
    error: list[BaseException] = []

    def _exercise():
        try:
            with plans._plan_lock(record.id, "tst"):
                # Nested re-entry on the SAME path: must pass through the
                # depth>0 fast-path, not re-grab the OS lock.
                with plans._plan_lock(record.id, "tst"):
                    pass
            done.set()
        except BaseException as exc:  # noqa: BLE001 — surface to assert
            error.append(exc)
            done.set()

    t = threading.Thread(target=_exercise, daemon=True)
    t.start()
    finished = done.wait(timeout=10.0)

    assert finished, "nested re-entrant _plan_lock self-deadlocked"
    assert not error, f"nested lock raised: {error!r}"


def test_reentrant_lock_keys_match_across_existence(symlinked_vault):
    # Direct assertion on the keying invariant the fix restores: the key
    # the lock derives must be identical whether or not the lock file
    # exists on disk.
    record = _make_plan()
    code = vault.validate_project_code("tst")
    lock_path = plans._plans_dir(code) / f"{record.id}.lock"

    assert not lock_path.exists()
    key_before = str(lock_path.resolve())
    lock_path.touch()
    key_after = str(lock_path.resolve())
    assert key_before == key_after


# ── Finding 2: negative budget caps degrade to unbounded ────────────────


def test_negative_token_cap_coerces_to_none():
    # Pre-fix: int(-1) returned as-is. Fix: < 0 → None (unbounded).
    assert plans._coerce_cap(-1, int) is None
    assert plans._coerce_cap(-5, float) is None


def test_zero_cap_is_preserved():
    # Guard the boundary: 0 is a legitimate (if tight) cap, not negative.
    assert plans._coerce_cap(0, int) == 0
    assert plans._coerce_cap(0.0, float) == 0.0


def test_positive_cap_unaffected():
    assert plans._coerce_cap(1000, int) == 1000
    assert plans._coerce_cap("2.5", float) == 2.5


def test_negative_cap_in_frontmatter_loads_as_unbounded(symlinked_vault):
    # End-to-end through persist/load: a hand-edited negative cap in the
    # frontmatter must load as None (unbounded) rather than instant-halt.
    record = _make_plan()
    target = plans._plans_dir(
        vault.validate_project_code("tst")
    ) / f"{record.id}.md"
    raw = target.read_text(encoding="utf-8")
    # Inject a negative cap into the frontmatter (after the opening '---').
    raw = raw.replace("---\n", "---\nmax_tokens: -1\n", 1)
    target.write_text(raw, encoding="utf-8")

    reloaded = plans.load(record.id, "tst")
    assert reloaded is not None
    assert reloaded.max_tokens is None
