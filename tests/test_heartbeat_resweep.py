"""0.9.0 pre-ship re-sweep regressions for ``heartbeat.py``.

Two confirmed findings:

- Finding 1 (race): the dispatch-failure retry path must not resurrect an
  operator-cancelled task back to ``pending`` (which ``_select_next_pending``
  would re-dispatch, defeating the cancel).
- Finding 2 (correctness): a ``depends_on`` suffix must not false-satisfy
  against an unrelated done task via a bare ``endswith`` over the whole id.
"""

from __future__ import annotations

import pytest

from modulatio import config, heartbeat


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    yield


# === Finding 1: retry path must honor a mid-dispatch cancel ===

def test_retry_path_does_not_resurrect_cancelled_task():
    """A retryable dispatch failure that races with an operator cancel must
    leave the task ``cancelled`` — NOT re-arm it to ``pending``.

    The callback cancels the task out-of-band (as an operator would) and then
    raises, forcing the retries-remaining branch. Before the fix that branch
    used a plain ``update_task(status='pending')`` which clobbered the cancel.
    """
    task = heartbeat.add_task(
        description="x", project_code="STA", objective="o", max_retries=3,
    )

    def cancel_then_fail(project_code, objective, **kwargs):
        # Operator cancels the in-flight task under the lock, then dispatch fails.
        heartbeat.cancel_task(task["id"])
        raise RuntimeError("dispatch boom")

    hb = heartbeat.Heartbeat(dispatch_callback=cancel_then_fail)
    hb._run_task(dict(task))

    after = heartbeat.get_task(task["id"])
    assert after["status"] == "cancelled"
    # And it must NOT be re-selectable for dispatch.
    assert heartbeat.next_pending() is None


def test_retry_path_still_rearms_when_not_cancelled():
    """Guard the back-compat: a plain retryable failure (no cancel) must still
    re-arm the task to ``pending`` with bumped retries for the next tick."""
    task = heartbeat.add_task(
        description="x", project_code="STA", objective="o", max_retries=3,
    )

    def just_fail(project_code, objective, **kwargs):
        raise RuntimeError("dispatch boom")

    hb = heartbeat.Heartbeat(dispatch_callback=just_fail)
    hb._run_task(dict(task))

    after = heartbeat.get_task(task["id"])
    assert after["status"] == "pending"
    assert after["retries"] == 1
    assert after["started"] is None
    # Re-armed → selectable again.
    assert heartbeat.next_pending()["id"] == task["id"]


# === Finding 2: dependency suffix must not false-match an unrelated id ===

def test_short_dep_does_not_false_match_unrelated_done_task():
    """A short ``depends_on`` fragment that coincidentally tails an unrelated
    done task id must NOT satisfy the dependency.

    ``done`` is some completed task; we derive a short suffix from ITS id and
    attach it as a dependency on a different pending task. Before the fix the
    bare ``endswith`` satisfied it, dispatching the dependent prematurely.
    """
    done = heartbeat.add_task(description="dep", project_code="STA", objective="o")
    heartbeat.update_task(done["id"], status="done")

    # A short tail of the unrelated done id (shorter than the 6-hex token).
    short_suffix = done["id"][-2:]
    waiter = heartbeat.add_task(
        description="waiter",
        project_code="STA",
        objective="o2",
        depends_on=[short_suffix],
    )

    selected = heartbeat.next_pending()
    # The waiter's dep is unsatisfied (no done task whose id matches the FULL
    # token boundary for this fragment), so it must not be selected.
    assert selected is None or selected["id"] != waiter["id"]


def test_full_token_suffix_still_satisfies_dependency():
    """The documented safe case: a suffix spanning the full 6-hex random token
    of the actual predecessor still satisfies the dependency."""
    done = heartbeat.add_task(description="dep", project_code="STA", objective="o")
    heartbeat.update_task(done["id"], status="done")

    full_token = done["id"][-6:]  # the secrets.token_hex(3) segment
    waiter = heartbeat.add_task(
        description="waiter",
        project_code="STA",
        objective="o2",
        depends_on=[full_token],
    )

    selected = heartbeat.next_pending()
    assert selected is not None
    assert selected["id"] == waiter["id"]


def test_full_id_dep_satisfies_dependency():
    """An exact full-id dependency is honored."""
    done = heartbeat.add_task(description="dep", project_code="STA", objective="o")
    heartbeat.update_task(done["id"], status="done")
    waiter = heartbeat.add_task(
        description="waiter",
        project_code="STA",
        objective="o2",
        depends_on=[done["id"]],
    )
    selected = heartbeat.next_pending()
    assert selected is not None and selected["id"] == waiter["id"]
