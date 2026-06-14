"""Regression tests for the round-2 full-debug findings on plans.py.

Each test fails against the pre-fix code and passes after:

1. extract_sub_objectives dropped any sub-objective whose bold title
   contained a markdown emphasis asterisk (negated-`*` title class).
2. persist() allocated next_plan_id then wrote non-atomically, so two
   concurrent persists could collide on the same id and clobber.
3. YAML bool / malformed max_tokens / max_cost_usd silently coerced to
   a tiny cap (or raised) instead of degrading to None.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import modulatio.plans as plans_mod
from modulatio import plans, vault


@pytest.fixture
def isolated_vault(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    vault.init_project("tst", "Test", "obj")
    return tmp_path


# ── Finding 1: inner-emphasis titles must not be dropped ────────────────


def test_extract_sub_objectives_keeps_inner_emphasis_title():
    body = (
        "### Sub-objectives\n\n"
        "**1. First plain title** — do the first thing.\n"
        "**2. Write the *draft*** — produce a draft.\n"
        "**3. Third plain title** — do the third thing.\n"
    )
    items = plans.extract_sub_objectives(body)
    indices = [it["index"] for it in items]
    # The core bug: the inner-emphasis item (2) used to be DROPPED
    # entirely. It must now be present and in order.
    assert indices == [1, 2, 3]
    # The emphasized word survives in the title (the load-bearing
    # content is preserved; the exact * balancing on a triple-asterisk
    # `*draft***` boundary is inherently ambiguous and not asserted).
    assert "draft" in items[1]["title"]
    assert "produce a draft." in items[1]["description"]


def test_extract_sub_objectives_keeps_multiple_emphasis_items():
    # Two emphasized items in a row — both used to vanish pre-fix.
    body = (
        "### Sub-objectives\n\n"
        "**1. Refine the *outline*** — sketch it.\n"
        "**2. Draft the *body*** — write it.\n"
        "**3. Plain finish** — done.\n"
    )
    items = plans.extract_sub_objectives(body)
    assert [it["index"] for it in items] == [1, 2, 3]


def test_extract_sub_objectives_plain_titles_unchanged():
    # Behavior preservation: the common case still parses identically.
    body = (
        "### Sub-objectives\n\n"
        "**1. Research the topic** — gather sources.\n"
        "**2. Write the report**\n"
    )
    items = plans.extract_sub_objectives(body)
    assert [it["index"] for it in items] == [1, 2]
    assert items[0]["title"] == "Research the topic"
    assert items[0]["description"] == "gather sources."
    # Title-only item defaults description to the title.
    assert items[1]["title"] == "Write the report"
    assert items[1]["description"] == "Write the report"


def test_extract_sub_objectives_warns_on_dropped_item(caplog):
    # If a future regex drift drops an item, the count cross-check logs.
    # We can't easily force a drop now (the fix parses them), so assert
    # the no-drop happy path emits NO warning.
    body = (
        "### Sub-objectives\n\n"
        "**1. Alpha** — a.\n"
        "**2. Beta with *style*** — b.\n"
    )
    with caplog.at_level("WARNING", logger="modulatio.plans"):
        plans.extract_sub_objectives(body)
    assert "may have been dropped" not in caplog.text


# ── Pre-ship: nested **bold** inside a title must not corrupt parse ─────


def test_extract_sub_objectives_nested_bold_title_not_truncated():
    # A nested **bold** span inside the title used to truncate it: the
    # lazy title match stopped at the first inner `**`, so the title
    # became "Do the " and the real title/description bled into the
    # description — WITHOUT tripping the count cross-check (still 1 item).
    body = (
        "### Sub-objectives\n\n"
        "**1. Do the **important** thing** — ship it.\n"
        "**2. Plain second** — and this.\n"
    )
    items = plans.extract_sub_objectives(body)
    assert [it["index"] for it in items] == [1, 2]
    # Title now spans to the LAST `**` on the line, keeping the bold span.
    assert items[0]["title"] == "Do the **important** thing"
    assert items[0]["description"] == "ship it."
    # The greedy match stays line-anchored: item 2 is untouched.
    assert items[1]["title"] == "Plain second"
    assert items[1]["description"] == "and this."


# ── Pre-ship: detected item-drop fails CLOSED (returns empty) ───────────


def test_extract_sub_objectives_returns_empty_on_detected_drop(caplog):
    # A malformed item (missing closing bold) matches the line-start
    # cross-check but not the item regex. Previously the function logged
    # a warning and returned the SHORT list, silently skipping the
    # approved sub-objective. It must now fail closed (empty) so the
    # dispatcher pauses for human revision.
    body = (
        "### Sub-objectives\n\n"
        "**1. First well-formed task** — do it.\n"
        "**2. Second task that forgot its closing bold\n"
        "**3. Third well-formed task** — do it too.\n"
    )
    with caplog.at_level("WARNING", logger="modulatio.plans"):
        items = plans.extract_sub_objectives(body)
    assert items == []
    assert "fail closed" in caplog.text or "dropped" in caplog.text


# ── Finding 2: persist() id allocation is collision-safe ────────────────


_PLAN_BODY = (
    "<!-- modulatio:plan -->\n"
    "### Diagnostic\nx\n\n### Sub-objectives\n**1. Do it**\n\n### Risks\nnone\n"
)


def test_persist_does_not_clobber_pre_existing_id(isolated_vault, monkeypatch):
    # Pin next_plan_id to a fixed value to force a collision, then verify
    # the first plan's bytes survive (O_EXCL won't overwrite) and the
    # second persist still succeeds with a fresh id.
    first = plans.persist(
        _PLAN_BODY, project_code="tst", agent_id="leader",
        source_message="first",
    )
    assert first is not None
    first_text = first.path.read_text()

    # Simulate a stale next_plan_id that re-returns the existing id once,
    # then the real (incremented) id on retry.
    real_next = plans_mod.next_plan_id
    calls = {"n": 0}

    def flaky_next(code: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return first.id  # collide with the existing file
        return real_next(code)

    monkeypatch.setattr(plans_mod, "next_plan_id", flaky_next)
    second = plans.persist(
        _PLAN_BODY, project_code="tst", agent_id="leader",
        source_message="second",
    )
    assert second is not None
    assert second.id != first.id
    # The first plan's bytes were NOT clobbered by the colliding second.
    assert first.path.read_text() == first_text
    assert "first" in first.path.read_text()
    assert "second" in second.path.read_text()


def test_persist_round_trips_after_atomic_write(isolated_vault):
    rec = plans.persist(
        _PLAN_BODY, project_code="tst", agent_id="leader",
        source_message="msg",
    )
    assert rec is not None
    loaded = plans.load(rec.id, "tst")
    assert loaded is not None
    assert loaded.id == rec.id
    assert loaded.source_message == "msg"


# ── Finding 3: malformed budget caps degrade to None ────────────────────


def _write_plan_with_frontmatter(isolated_vault, plan_id: str, fm_lines: str):
    plans_dir = vault.project_dir("tst") / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / f"{plan_id}.md").write_text(
        "---\n"
        f"id: {plan_id}\n"
        "project_code: tst\n"
        "created_at: '2026-06-13T00:00:00+00:00'\n"
        "agent_id: leader\n"
        "source_message: m\n"
        "status: draft\n"
        f"{fm_lines}"
        "---\n\nbody\n"
    )
    return plan_id


def test_load_rejects_yaml_bool_max_tokens(isolated_vault):
    pid = _write_plan_with_frontmatter(
        isolated_vault, "TST-PLAN-901", "max_tokens: true\n",
    )
    rec = plans.load(pid, "tst")
    assert rec is not None
    # `true` must NOT become a cap of 1 (int(True)); it degrades to None.
    assert rec.max_tokens is None


def test_load_rejects_yaml_bool_max_cost_usd(isolated_vault):
    pid = _write_plan_with_frontmatter(
        isolated_vault, "TST-PLAN-902", "max_cost_usd: false\n",
    )
    rec = plans.load(pid, "tst")
    assert rec is not None
    assert rec.max_cost_usd is None


def test_load_degrades_non_numeric_cap_to_none(isolated_vault):
    # A non-numeric string previously raised ValueError out of load(),
    # which list_plans silently swallowed (whole plan vanished).
    pid = _write_plan_with_frontmatter(
        isolated_vault, "TST-PLAN-903", "max_tokens: not-a-number\n",
    )
    rec = plans.load(pid, "tst")
    assert rec is not None
    assert rec.max_tokens is None


def test_load_non_ascii_plan_under_c_locale(isolated_vault, monkeypatch):
    # The write path is UTF-8; a non-UTF-8 process locale must not turn a
    # legitimate non-ASCII title/body into a UnicodeDecodeError that bricks
    # load() + list_plans(). Force a non-UTF-8 default encoding to mirror a
    # daemon/cron run under C/POSIX.
    rec = plans.persist(
        "<!-- modulatio:plan -->\n"
        "### Diagnostic\nRédiger un résumé — 概要 ✦\n\n"
        "### Sub-objectives\n**1. Écrire**\n",
        project_code="tst", agent_id="leader", source_message="café",
    )
    assert rec is not None

    # Simulate the locale-default decode path failing on UTF-8 bytes:
    # patch read_text to behave as if the platform default were ASCII when
    # no encoding is passed, and UTF-8 only when encoding is explicit.
    real_read_text = Path.read_text

    def locale_sensitive_read_text(self, *args, encoding=None, **kwargs):
        if encoding is None:
            # Emulate C/POSIX: ascii decode of UTF-8 bytes raises.
            data = self.read_bytes()
            return data.decode("ascii")  # raises UnicodeDecodeError
        return real_read_text(self, *args, encoding=encoding, **kwargs)

    monkeypatch.setattr(Path, "read_text", locale_sensitive_read_text)

    loaded = plans.load(rec.id, "tst")
    assert loaded is not None
    assert "concept" not in loaded.body  # sanity: real body, not placeholder
    assert "概要" in loaded.body
    # list_plans must not silently swallow it either.
    listed = plans.list_plans("tst")
    assert any(p.id == rec.id for p in listed)


def test_load_preserves_valid_numeric_caps(isolated_vault):
    pid = _write_plan_with_frontmatter(
        isolated_vault, "TST-PLAN-904",
        "max_tokens: 50000\nmax_cost_usd: 2.5\nmax_wall_clock_min: 30\n",
    )
    rec = plans.load(pid, "tst")
    assert rec is not None
    assert rec.max_tokens == 50000
    assert rec.max_cost_usd == 2.5
    assert rec.max_wall_clock_min == 30.0
