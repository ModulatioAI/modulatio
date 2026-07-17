"""Tests for vault run helpers (per-kickoff isolation).

The vault adds three new helpers alongside ``init_project``:
- ``run_dir(code, run_id)`` — pure path resolver
- ``init_run(code, run_id, objective)`` — creates the run subfolder
- ``list_runs(code)`` — sorted list of existing runs
- ``generate_run_id()`` — sortable UTC timestamp

Together they implement per-kickoff isolation: every kickoff gets its
own folder under ``<project>/runs/<run_id>/`` so artifacts/goals/
tickets/decisions/reports/research from one run don't pollute the
next. Persistent state (agents, skills, standards, memory,
qc-history, qc-notes) stays at project root and is read by every run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import vault


@pytest.fixture
def isolated_vault(tmp_path: Path, monkeypatch):
    """Redirect VAULT_ROOT into a tmp_path so the test owns the layout."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    return tmp_path


def test_runs_dir_resolves_under_project_root(isolated_vault):
    """``runs_dir`` is purely structural — under each project at
    ``<project>/runs/``. Doesn't create the directory."""
    p = vault.runs_dir("FOO")
    assert p == vault.project_dir("FOO") / "runs"
    assert not p.exists()


def test_run_dir_resolves_per_run_subfolder(isolated_vault):
    """``run_dir(code, run_id)`` returns the runtime workspace path
    for one specific kickoff. Pure resolver — doesn't touch disk."""
    p = vault.run_dir("FOO", "20260428T140000Z")
    assert p == vault.project_dir("FOO") / "runs" / "20260428T140000Z"
    assert not p.exists()


def test_generate_run_id_is_sortable_with_uniqueness_suffix():
    """run_id format: ``YYYYMMDDTHHMMSSZ-<hex6>``. Sortable
    (timestamp dominates), unique within a second (suffix), no
    filesystem-hostile chars. Two calls in the same second must
    produce different ids — the test fixture path that drove this
    requirement.

    Suffix bumped to 6 hex chars 2026-04-30 after a single flaky
    full-suite run; 4 hex chars (65k slots) had ~1% birthday-paradox
    collision probability across this loop's 50 calls.
    """
    rid = vault.generate_run_id()
    # 16-char timestamp + '-' + 6-char hex = 23 chars.
    assert len(rid) == 23
    assert "T" in rid
    assert "-" in rid
    for bad in (":", "/", "\\", " "):
        assert bad not in rid

    # Uniqueness in a tight loop. With 16M slots, 50 same-second draws
    # collide ~0.008% of the time — well below any realistic flake rate.
    ids = {vault.generate_run_id() for _ in range(50)}
    assert len(ids) == 50, "uniqueness suffix isn't preventing collisions"


def test_init_run_creates_run_subfolder_with_run_subdirs(isolated_vault):
    """init_run creates ``runs/<run_id>/`` plus every directory in
    ``RUN_SUBDIRS``: goals, tasks, decisions, reports.
    PROJECT_SUBDIRS (agents/skills/artifacts/tickets/logs/research/etc.) are NOT
    created here — those live at the project root and accumulate."""
    vault.init_project("FOO", "Foo project", "objective")
    rid = "20260428T143000Z"
    target = vault.init_run("FOO", rid, "fetch BTC price")

    assert target.exists()
    assert target == vault.run_dir("FOO", rid)
    for sub in vault.RUN_SUBDIRS:
        assert (target / sub).is_dir(), f"missing run subdir {sub!r}"
    # Confirm no project-scoped subdirs leaked into the run folder.
    for sub in vault.PROJECT_SUBDIRS:
        assert not (target / sub).exists(), (
            f"project-scoped subdir {sub!r} should not be in run folder"
        )


def test_init_run_writes_objective_file(isolated_vault):
    """The kickoff's objective text is captured in
    ``runs/<id>/objective.md`` so a future reader can tell what this
    run was for. Includes run_id + creation timestamp in frontmatter."""
    vault.init_project("FOO", "Foo", "x")
    target = vault.init_run("FOO", "20260428T143000Z", "Build a thing")
    obj = (target / "objective.md").read_text()
    assert "Build a thing" in obj
    assert "20260428T143000Z" in obj
    assert "created:" in obj


def test_init_run_raises_on_duplicate_unless_exist_ok(isolated_vault):
    """Re-initializing the same run_id collides — caller most likely
    intended a fresh run. ``exist_ok=True`` opts into idempotency for
    resumption flows."""
    vault.init_project("FOO", "Foo", "x")
    vault.init_run("FOO", "20260428T143000Z", "first")
    with pytest.raises(FileExistsError):
        vault.init_run("FOO", "20260428T143000Z", "second")
    # exist_ok skips the guard.
    vault.init_run("FOO", "20260428T143000Z", "second", exist_ok=True)


def test_init_run_requires_project_to_exist(isolated_vault):
    """Defensive: init_run with a missing project root raises rather
    than creating an orphaned runs/ tree without a parent."""
    with pytest.raises(FileNotFoundError):
        vault.init_run("FOO", "20260428T143000Z", "x")


def test_list_runs_returns_chronological_order(isolated_vault):
    """list_runs sorts run_ids lex (works because timestamps sort
    chronologically). Empty when nothing's been run yet."""
    vault.init_project("FOO", "Foo", "x")
    assert vault.list_runs("FOO") == []
    vault.init_run("FOO", "20260428T100000Z", "a")
    vault.init_run("FOO", "20260428T140000Z", "b")
    vault.init_run("FOO", "20260428T120000Z", "c")
    runs = vault.list_runs("FOO")
    assert runs == [
        "20260428T100000Z",
        "20260428T120000Z",
        "20260428T140000Z",
    ]


def test_list_runs_returns_empty_for_unknown_project(isolated_vault):
    """No project, no runs — return empty rather than raise. Lets
    callers do a single-line `if vault.list_runs(code): ...`."""
    assert vault.list_runs("NOPE") == []


def test_latest_run_returns_most_recent(isolated_vault):
    """``latest_run`` returns the lex-max run_id (= most recent
    timestamp). Used by TUI screens to default to "show me the latest
    kickoff's data" without requiring a manual picker."""
    vault.init_project("FOO", "Foo", "x")
    assert vault.latest_run("FOO") is None
    vault.init_run("FOO", "20260428T100000Z-aaaa", "early")
    vault.init_run("FOO", "20260428T140000Z-bbbb", "later")
    vault.init_run("FOO", "20260428T120000Z-cccc", "middle")
    assert vault.latest_run("FOO") == "20260428T140000Z-bbbb"


def test_latest_run_returns_none_for_unknown_project(isolated_vault):
    """No project → no runs → None. Caller-side ``if run_id is None:
    fall_back_to_project_root`` is the legacy-compat pattern."""
    assert vault.latest_run("NOPE") is None


def test_init_project_still_creates_all_subdirs_for_backcompat(isolated_vault):
    """Pre-run-isolation callers wrote directly under
    ``<project>/<subdir>/``. We can't break them — init_project still
    creates every subdir at project root. Per-run isolation is opt-in
    by threading a run_id through. This assertion guards the back-
    compat surface."""
    root = vault.init_project("FOO", "Foo", "x")
    for sub in vault.SUBDIRS:
        assert (root / sub).is_dir(), f"init_project missed {sub!r}"


def test_project_subdirs_and_run_subdirs_partition_subdirs(isolated_vault):
    """Sanity: PROJECT_SUBDIRS + RUN_SUBDIRS == SUBDIRS, no overlap.
    Catches accidental drift if someone adds a new subdir to one
    list without checking the other."""
    p = set(vault.PROJECT_SUBDIRS)
    r = set(vault.RUN_SUBDIRS)
    assert p.isdisjoint(r), f"overlap: {p & r}"
    assert p | r == set(vault.SUBDIRS)


def test_migrate_legacy_run_layout_lifts_tickets_and_artifacts(isolated_vault):
    """One-time migration (M4): pre-durable-layout data kept UNDER a
    run folder (runs/<id>/{tickets,artifacts}) is lifted to the project-durable
    locations, without clobbering, and is idempotent (a no-op once done)."""
    vault.init_project("MIG", "Migrate", "obj")
    proj = vault.project_dir("MIG")
    rid = "20260101T000000Z-abc123"
    # Simulate the OLD layout: durable data nested under the run folder.
    (proj / "runs" / rid / "tickets").mkdir(parents=True)
    (proj / "runs" / rid / "tickets" / "MIG-1.md").write_text("old ticket\n")
    (proj / "runs" / rid / "artifacts" / "drafts").mkdir(parents=True)
    (proj / "runs" / rid / "artifacts" / "drafts" / "x.md").write_text("# old\n")

    moved = vault.migrate_legacy_run_layout("MIG")
    assert moved >= 2
    # Tickets lifted to the project root; artifacts to the run-namespaced tree.
    assert (proj / "tickets" / "MIG-1.md").exists()
    assert not (proj / "runs" / rid / "tickets").exists()
    assert (proj / "artifacts" / rid / "drafts" / "x.md").exists()
    assert not (proj / "runs" / rid / "artifacts").exists()
    # Idempotent: nothing left to move.
    assert vault.migrate_legacy_run_layout("MIG") == 0


def test_migrate_legacy_run_layout_no_clobber(isolated_vault):
    """A legacy file whose name already exists at the destination is left in
    place (never overwrites the durable copy)."""
    vault.init_project("MIG2", "Migrate", "obj")
    proj = vault.project_dir("MIG2")
    rid = "20260101T000000Z-dad222"
    (proj / "tickets").mkdir(parents=True, exist_ok=True)
    (proj / "tickets" / "MIG2-1.md").write_text("DURABLE COPY\n")
    (proj / "runs" / rid / "tickets").mkdir(parents=True)
    (proj / "runs" / rid / "tickets" / "MIG2-1.md").write_text("LEGACY COPY\n")

    vault.migrate_legacy_run_layout("MIG2")
    # Durable copy is untouched; legacy copy was not moved over it.
    assert (proj / "tickets" / "MIG2-1.md").read_text() == "DURABLE COPY\n"


def test_research_is_project_durable_not_run_scoped(isolated_vault):
    """The research CACHE is a project-durable library (research.py writes it to
    <project>/research), so ``research`` belongs to PROJECT_SUBDIRS, not
    RUN_SUBDIRS — otherwise the subdir list contracts a per-run location the
    research module never uses."""
    assert "research" in vault.PROJECT_SUBDIRS
    assert "research" not in vault.RUN_SUBDIRS
    vault.init_project("RES", "Research", "obj")
    root = vault.project_dir("RES")
    run = vault.init_run("RES", "20260630T120000Z", "obj")
    assert (root / "research").is_dir()            # project-durable
    assert not (run / "research").exists()         # not created per-run


def test_durable_kinds_are_project_scoped_not_run(isolated_vault):
    """artifacts, tickets, logs are the project's DURABLE record — they
    persist + accumulate across every run, so they belong to PROJECT_SUBDIRS,
    never RUN_SUBDIRS. The run folder holds only this kickoff's transient
    execution trace (goals/tasks/decisions/reports)."""
    for kind in ("artifacts", "tickets", "logs"):
        assert kind in vault.PROJECT_SUBDIRS, f"{kind} must be project-durable"
        assert kind not in vault.RUN_SUBDIRS, f"{kind} must not be run-scoped"
    # init_project creates them at the project root; init_run does not.
    vault.init_project("DUR", "Durable", "obj")
    root = vault.project_dir("DUR")
    run = vault.init_run("DUR", "20260630T120000Z", "obj")
    for kind in ("artifacts", "tickets", "logs"):
        assert (root / kind).is_dir(), f"project missing durable {kind!r}"
        assert not (run / kind).exists(), f"run folder should not hold {kind!r}"


# === list_projects + _is_project_dir (marker-aware enumeration) ===


def test_is_project_dir_requires_seed_markers(isolated_vault):
    """A vault child is a project only if its name is a valid code AND it
    carries the Modulatio seed markers (index.md + comptroller.md). Drop a
    marker and it stops counting — the guard that keeps a stray folder out
    of a switch/delete list."""
    root = vault.init_project("gamma", "Gamma", "z")
    assert vault._is_project_dir(root)
    (root / "comptroller.md").unlink()
    assert not vault._is_project_dir(root)


def test_list_projects_excludes_unmarked_stray_dirs(isolated_vault):
    """list_projects returns only real projects, sorted. A stray dir with a
    valid-looking name but no seed markers (a repo clone, an Obsidian note
    folder) is excluded, so it can never surface as a deletable project —
    and an invalid-cased name is excluded too."""
    vault.init_project("alpha", "Alpha", "x")
    vault.init_project("beta_1", "Beta", "y")
    stray = vault.VAULT_ROOT / "notes"  # valid code shape, but not a project
    stray.mkdir(parents=True)
    (stray / "readme.md").write_text("not a project", encoding="utf-8")
    (vault.VAULT_ROOT / "Repo").mkdir()  # invalid (uppercase) name

    assert vault.list_projects() == ["alpha", "beta_1"]


def test_list_projects_empty_when_root_missing(isolated_vault):
    """No vault root yet → empty list, not an error (fresh install)."""
    assert not vault.VAULT_ROOT.exists()
    assert vault.list_projects() == []


def test_is_project_dir_rejects_symlinked_vault_child(isolated_vault, tmp_path):
    """A vault child that is a SYMLINK to an outside dir carrying planted
    markers is NOT a project — it must not surface in
    list_projects, so delete can never reach an outside tree through it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.md").write_text("x", encoding="utf-8")
    (outside / "comptroller.md").write_text("x", encoding="utf-8")
    vault.VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    link = vault.VAULT_ROOT / "evil"
    link.symlink_to(outside, target_is_directory=True)

    assert not vault._is_project_dir(link)
    assert "evil" not in vault.list_projects()


# === validate_project_code (SEC-004 trust-boundary regex) ===

class TestValidateProjectCode:
    """Strict validator that rejects path-traversal, shell-hostile chars,
    and codes outside the canonical [a-z][a-z0-9_]{0,31} shape. Single
    source of truth for setup wizard, heartbeat queue, vault path
    resolver, and any future Telegram/CLI entry point.
    """

    @pytest.mark.parametrize("ok", ["a", "tst", "modulatio1", "my_book", "q3_marketing", "x" * 32])
    def test_accepts_valid_codes(self, ok):
        assert vault.validate_project_code(ok) == ok

    @pytest.mark.parametrize("bad", [
        "",                       # empty
        "X",                      # uppercase — strict
        "MyBook",                 # mixed case
        "1foo",                   # starts with digit
        "_foo",                   # starts with underscore
        "foo-bar",                # hyphen not allowed
        "foo bar",                # space
        "foo.bar",                # dot
        "foo/bar",                # slash — path traversal
        "../etc",                 # parent traversal
        "..",                     # parent traversal
        "/abs",                   # absolute
        "foo\x00bar",             # null byte
        "foo\nbar",               # newline
        "x" * 33,                 # too long
    ])
    def test_rejects_invalid(self, bad):
        with pytest.raises(ValueError):
            vault.validate_project_code(bad)

    def test_rejects_non_str(self):
        with pytest.raises(ValueError):
            vault.validate_project_code(None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            vault.validate_project_code(123)  # type: ignore[arg-type]


def test_project_dir_rejects_traversal(isolated_vault):
    """vault.project_dir is the path-resolution sink; it must reject
    traversal components even from internal callers (defense in depth).
    """
    with pytest.raises(ValueError):
        vault.project_dir("../etc")
    with pytest.raises(ValueError):
        vault.project_dir("foo/bar")


def test_project_dir_lowercases_for_legacy_callers(isolated_vault):
    """project_dir is permissive on case so legacy uppercase codes
    (e.g. heartbeat queue records storing .upper()) still resolve.
    """
    p = vault.project_dir("TST")
    assert p.name == "tst"


# === run_id traversal validation =========


def test_validate_run_id_accepts_generated_format():
    """The output of `generate_run_id()` MUST pass validation —
    otherwise we'd reject our own ids."""
    rid = vault.generate_run_id()
    vault.validate_run_id(rid)  # no exception


def test_validate_run_id_accepts_legacy_test_shapes():
    """Legacy/test ids that don't match the strict timestamp format
    (4-hex suffix, plain `run-001`, etc.) must still pass — the
    check is character-based, not format-based."""
    for legacy in ("run-001", "20260428T120000Z-rrrr", "test_run", "abc123"):
        vault.validate_run_id(legacy)


@pytest.mark.parametrize(
    "bad",
    [
        "../etc/passwd",
        "..",
        "foo/../bar",
        "/absolute/path",
        "a\\b",
        "evil\x00null",
        ".hidden",
        "",
    ],
)
def test_validate_run_id_rejects_unsafe(bad: str) -> None:
    """each of these would have
    let the resulting `run_dir(...)` path escape (or land in) a
    location we don't want."""
    with pytest.raises(ValueError):
        vault.validate_run_id(bad)


def test_validate_run_id_rejects_too_long():
    with pytest.raises(ValueError, match="too long"):
        vault.validate_run_id("x" * 200)


def test_run_dir_rejects_traversal(isolated_vault):
    """`run_dir` calls `validate_run_id` first; a traversal attempt
    raises before any path object is built."""
    with pytest.raises(ValueError, match="unsafe"):
        vault.run_dir("TST", "../../etc")


def test_run_dir_accepts_legitimate_id(isolated_vault):
    """Round-trip: a legitimate id resolves to the expected location
    under runs_dir(code), and stays inside the runs folder."""
    rid = "20260428T120000Z-abcdef"
    p = vault.run_dir("TST", rid)
    assert p.parent == vault.runs_dir("TST")
    assert p.name == rid


def test_run_dir_resolved_bounds_check(isolated_vault):
    """If `<vault>/projects/<code>/runs/<run_id>` is a symlink that
    points outside the runs folder, `run_dir` must reject it.
    Defense in depth above the character-based check — same shape
    as `tools._is_safe_relative_file_arg` for artifact writes."""
    runs = vault.runs_dir("TST")
    runs.mkdir(parents=True, exist_ok=True)
    outside = isolated_vault / "outside"
    outside.mkdir()
    (runs / "evil").symlink_to(outside)
    with pytest.raises(ValueError, match="resolves outside"):
        vault.run_dir("TST", "evil")


# === delete_run — guarded single-run delete (Feng-Tui JOBS tab) =============


def test_delete_run_removes_the_whole_run_folder(isolated_vault):
    vault.init_project("STA", "x", "obj")
    rid = "20260101T010101Z-aaa111"
    vault.init_run("STA", rid, "do a thing")
    assert vault.run_dir("STA", rid).exists()
    assert vault.delete_run("STA", rid) is True
    assert not vault.run_dir("STA", rid).exists()
    assert rid not in vault.list_runs("STA")


def test_delete_run_missing_run_returns_false(isolated_vault):
    vault.init_project("STA", "x", "obj")
    assert vault.delete_run("STA", "20260101T010101Z-nope11") is False


def test_delete_run_validates_run_id(isolated_vault):
    vault.init_project("STA", "x", "obj")
    with pytest.raises(Exception):
        vault.delete_run("STA", "../escape")


def test_delete_run_refuses_a_symlinked_run(isolated_vault, tmp_path):
    vault.init_project("STA", "x", "obj")
    outside = tmp_path / "outside_target"
    outside.mkdir()
    rid = "20260101T010101Z-bbb222"
    link = vault.runs_dir("STA") / rid
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    with pytest.raises(Exception):
        vault.delete_run("STA", rid)
    assert outside.exists()  # the symlink target is never followed/deleted


def test_run_size_sums_files_skips_symlinks(isolated_vault, tmp_path):
    vault.init_project("STA", "x", "obj")
    run = vault.init_run("STA", "20260101T010101Z-size01", "obj")
    base = vault.run_size(run)  # objective.md etc. from init_run
    (run / "a.txt").write_text("hello", encoding="utf-8")  # 5 bytes
    (run / "b.txt").write_text("world!", encoding="utf-8")  # 6 bytes
    outside = tmp_path / "big"
    outside.write_bytes(b"x" * 1000)
    (run / "link").symlink_to(outside)  # must NOT be counted
    assert vault.run_size(run) == base + 11


def test_run_size_tolerates_vanished_file(isolated_vault):
    vault.init_project("STA", "x", "obj")
    run = vault.init_run("STA", "20260101T010101Z-size02", "obj")
    # A missing dir returns 0, never raises (concurrent-delete tolerance).
    assert vault.run_size(run / "gone") == 0


def test_human_size_transitions():
    assert vault.human_size(0) == "0 B"
    assert vault.human_size(512) == "512 B"
    assert vault.human_size(512000) == "500.0 KB"
    assert vault.human_size(5 * 1024 * 1024) == "5.0 MB"
    assert vault.human_size(3 * 1024 ** 3) == "3.0 GB"
