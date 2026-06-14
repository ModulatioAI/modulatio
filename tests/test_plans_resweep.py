"""Re-sweep regression tests for two 0.9.0 pre-ship findings on plans.py.

Each test FAILS against the pre-fix code and PASSES after the fix.

Finding 1 (extract_sub_objectives, LOW/correctness):
    A nested ``**bold**`` span in the DESCRIPTION (after the em-dash)
    made the greedy title capture bind its closing ``**`` to the last
    ``**`` on the line, corrupting both title and description WITHOUT
    tripping the item-count cross-check (still one parsed item).

Finding 2 (set_status / update_execution_state, LOW/race):
    Both do an unguarded read-YAML / modify / write-YAML on the same
    plan file. A concurrent cancel()'s ``status='declined'`` flip could
    be clobbered by the dispatcher loop's update_execution_state writing
    back its (pre-cancel) meta — a lost update. The fix serializes both
    mutators under a per-plan flock and reads INSIDE the lock.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from modulatio import plans, vault


@pytest.fixture
def isolated_vault(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    vault.init_project("tst", "Test", "obj")
    return tmp_path


def _make_plan(project_code: str = "tst") -> "plans.PlanRecord":
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\n\n### Sub-objectives\n\n### Risks\n"
    )
    return plans.persist(
        body, project_code=project_code, agent_id="leader",
        source_message="please plan",
    )


# ── Finding 1: bold in the DESCRIPTION must not corrupt title/desc ──────


def test_bold_in_description_does_not_corrupt_title():
    # The exact finding example: greedy capture used to bind the title's
    # closing ** to the ** around "care" in the description, yielding a
    # title of "Build the thing** — do it with **care" and dropping the
    # rest of the description.
    body = (
        "### Sub-objectives\n\n"
        "**1. Build the thing** — do it with **care** and precision.\n"
        "**2. Plain second** — and this.\n"
    )
    items = plans.extract_sub_objectives(body)
    assert [it["index"] for it in items] == [1, 2]
    assert items[0]["title"] == "Build the thing"
    assert items[0]["description"] == "do it with **care** and precision."
    # The second item must be untouched (no line bleed).
    assert items[1]["title"] == "Plain second"
    assert items[1]["description"] == "and this."


def test_bold_in_description_and_nested_bold_in_title_together():
    # Nested bold in the TITLE *and* bold in the description on the same
    # line: the title closes at the ** preceding the em-dash, the nested
    # title bold and the description bold both survive intact.
    body = (
        "### Sub-objectives\n\n"
        "**1. Do the **important** thing** — handle with **care** now.\n"
    )
    items = plans.extract_sub_objectives(body)
    assert len(items) == 1
    assert items[0]["title"] == "Do the **important** thing"
    assert items[0]["description"] == "handle with **care** now."


def test_colon_separated_description_with_bold():
    # A colon (not an em-dash) is also a valid separator; bold after it
    # must stay in the description, not pull the title boundary.
    body = (
        "### Sub-objectives\n\n"
        "**1. Ship it**: ship with **urgency** today.\n"
    )
    items = plans.extract_sub_objectives(body)
    assert len(items) == 1
    assert items[0]["title"] == "Ship it"
    assert items[0]["description"] == "ship with **urgency** today."


def test_title_only_nested_bold_still_preserved():
    # Behavior preservation: a title-only item with nested bold (no
    # description) keeps its nested bold — the title closes at the LAST **.
    body = (
        "### Sub-objectives\n\n"
        "**1. Do the **important** thing**\n"
    )
    items = plans.extract_sub_objectives(body)
    assert len(items) == 1
    assert items[0]["title"] == "Do the **important** thing"
    # Title-only item defaults description to the title.
    assert items[0]["description"] == "Do the **important** thing"


# ── Finding 2: mutators serialize under a per-plan lock ─────────────────


def test_update_execution_state_blocks_while_plan_lock_held(isolated_vault):
    # Proves update_execution_state participates in the per-plan lock: a
    # held lock must make the call wait. Pre-fix (no lock) it never waited.
    saved = _make_plan()
    # Generous timeout so the call would WAIT (not time out) on a held lock.
    import os
    os.environ["MODULATIO_PLAN_LOCK_TIMEOUT"] = "5"
    try:
        released = threading.Event()
        started = threading.Event()

        def hold_lock():
            with plans._plan_lock(saved.id, "tst"):
                started.set()
                # Hold briefly so the writer must wait for us.
                time.sleep(0.4)
            released.set()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        assert started.wait(2.0)

        t0 = time.monotonic()
        plans.update_execution_state(saved.id, "tst", current_index=1)
        elapsed = time.monotonic() - t0
        holder.join(5.0)
        assert released.is_set()
        # The write had to wait for the lock holder (~0.4s). Pre-fix the
        # call took no lock and returned essentially immediately.
        assert elapsed >= 0.2
    finally:
        os.environ.pop("MODULATIO_PLAN_LOCK_TIMEOUT", None)


def test_cancel_not_clobbered_by_concurrent_update(isolated_vault):
    # Lost-update integration test: the dispatcher loop's
    # update_execution_state and a user cancel run concurrently, many
    # rounds. The fix guarantees cancel's 'declined' is never resurrected
    # to 'executing' by an update that read pre-cancel meta. Pre-fix this
    # races and a resurrected 'executing' shows up.
    import os
    os.environ["MODULATIO_PLAN_LOCK_TIMEOUT"] = "5"
    try:
        for _ in range(30):
            saved = _make_plan()
            plans.set_status(
                saved.id, "tst", "executing", decided_by="dispatcher",
            )

            barrier = threading.Barrier(2)

            def do_update(pid=saved.id):
                barrier.wait()
                plans.update_execution_state(
                    pid, "tst", current_index=2, tokens_used=123,
                )

            def do_cancel(pid=saved.id):
                barrier.wait()
                plans.cancel(pid, "tst", decided_by="user")

            tu = threading.Thread(target=do_update)
            tc = threading.Thread(target=do_cancel)
            tu.start()
            tc.start()
            tu.join(5.0)
            tc.join(5.0)

            loaded = plans.load(saved.id, "tst")
            # cancel is terminal: once declined, no update may flip it back.
            assert loaded.status == "declined", (
                f"cancel was clobbered: status={loaded.status!r}"
            )
    finally:
        os.environ.pop("MODULATIO_PLAN_LOCK_TIMEOUT", None)
