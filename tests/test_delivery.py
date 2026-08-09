"""Finished-product delivery (2026-05-29): render Leader-tagged deliverables
to a real document (DOCX) with a human name from the document's own title,
placed under ~/Documents/Modulatio/<project>/. Covers naming, the delivery
dir + env override, render/placement, collision, and missing-source skip."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from modulatio import delivery
from modulatio.export import ExportResult
from modulatio.delivery import _is_prose_source
import gc
import warnings
from modulatio.delivery import _looks_binary


# ── human name from markdown title ───────────────────────────────────────

def test_name_from_h1():
    md = "# LLM Coding Harnesses: A Survey\n\nbody text"
    assert delivery.human_name_from_markdown(md, fallback="t-004") == \
        "LLM Coding Harnesses - A Survey"  # colon → ' -'


def test_name_falls_back_to_first_line_when_no_heading():
    md = "Quarterly Revenue Plan\n\nsome body"
    assert delivery.human_name_from_markdown(md, fallback="t-1") == "Quarterly Revenue Plan"


def test_name_empty_uses_fallback():
    assert delivery.human_name_from_markdown("\n\n   \n", fallback="t-009") == "t-009"


def test_name_degenerate_title_uses_fallback():
    # A doc that leads with a stray frontmatter fence ('---') and has no real
    # H1 — the first non-empty line ('---') sanitizes to all-dashes, which must
    # NOT become the filename ('---.docx'). Fall back to the supplied name.
    md = "---\n\n## Section 2: The barriers\n\nbody"
    assert delivery.human_name_from_markdown(
        md, fallback="ai-ml-cost-research"
    ) == "ai-ml-cost-research"


def test_name_rejects_producer_narration_uses_fallback():
    """A producer that opens with thinking-out-loud ("I now have all the
    citations... Let me write...") must NOT have that sentence become the
    filename — it reads as narration, so fall back to the Leader's clean title."""
    md = ("I now have all the citations I need. Let me write the corrected "
          "artifact with real references.\n\n## Findings\n\nbody")
    name = delivery.human_name_from_markdown(
        md, fallback="Coconut Oil and Cognitive Performance"
    )
    assert "citations" not in name.lower()
    assert name == "Coconut Oil and Cognitive Performance"


def test_name_keeps_a_real_title_even_if_it_ends_in_a_period():
    """A genuine short title is not mistaken for narration."""
    md = "# Q3 Sales Analysis\n\nbody"
    assert delivery.human_name_from_markdown(md, fallback="x") == "Q3 Sales Analysis"


def test_name_strips_illegal_chars():
    md = '# Report: Q3/Q4 <draft> "final"?'
    name = delivery.human_name_from_markdown(md, fallback="x")
    for ch in '\\/<>:"|?*':
        assert ch not in name
    assert name.startswith("Report -")  # colon handled, slash dropped


def test_name_length_capped():
    md = "# " + "word " * 60
    assert len(delivery.human_name_from_markdown(md, fallback="x")) <= delivery._MAX_NAME_LEN


def test_name_never_t_id_when_title_present():
    """A human should recognize the product by name, not by t-00X."""
    md = "# Beacon Operator's Guide\n\nTask ID: PROJ-T-004\n"
    assert delivery.human_name_from_markdown(md, fallback="PROJ-T-004") == "Beacon Operator's Guide"


# ── delivery dir ─────────────────────────────────────────────────────────

def test_delivery_root_default(monkeypatch):
    monkeypatch.delenv("MODULATIO_DELIVERY_DIR", raising=False)
    assert delivery.delivery_root() == Path.home() / "Documents" / "Modulatio"


def test_delivery_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path / "deliver"))
    assert delivery.delivery_root() == tmp_path / "deliver"


def test_project_delivery_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    assert delivery.project_delivery_dir("ABC") == tmp_path / "ABC"


# ── placement (export mocked — no pandoc dependency) ─────────────────────

@pytest.fixture
def _mock_export(monkeypatch):
    """Stub export_artifact to a touch-the-file no-op so placement/naming
    tests don't depend on pandoc."""
    def _fake(source, dest, fmt):
        Path(dest).write_text(f"rendered {Path(source).name} as {fmt}")
        return ExportResult(source=Path(source), dest=Path(dest), format=fmt, error=None)
    monkeypatch.setattr(delivery, "export_artifact", _fake)


def test_deliver_product_names_and_places(monkeypatch, tmp_path, _mock_export):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    src = tmp_path / "paper.md"
    src.write_text("# Annual Report 2026\n\nbody")
    dp = delivery.deliver_product(src, project_code="ACME", task_id="ACME-T-002")
    assert dp.error is None
    assert dp.dest == tmp_path / "ACME" / "Annual Report 2026.docx"
    assert dp.dest.exists()
    assert dp.name == "Annual Report 2026"


def test_sanitize_filename_strips_bidi_controls():
    """_sanitize_filename must strip Unicode bidi/RTL-override
    controls (as _job_slug already does), so an LLM-derived job-name fallback
    can't produce a display-deceptive filename stem."""
    out = delivery._sanitize_filename("report‮evil‏")
    assert "‮" not in out and "‏" not in out


def test_deliver_finished_products_titleless_uses_job_name(
    monkeypatch, tmp_path, _mock_export
):
    """Feature B: a deliverable with no usable H1 title (leads with a stray
    '---', long task description) is named from the LEADER'S JOB NAME, not a
    junk '---.docx' nor the giant sanitized description."""
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    src = tmp_path / "briefing.md"
    src.write_text("---\n\n## Section 2: barriers\n\nbody")  # no H1
    long_desc = "Assemble the three researched sections into final single doc"
    out = delivery.deliver_finished_products(
        [("T-004", src, long_desc, "document")],
        project_code="ACME", job_name="ai-ml-cost-research",
    )
    assert out[0].dest.name == "ai-ml-cost-research.docx"


def test_deliver_product_collision_disambiguates(monkeypatch, tmp_path, _mock_export):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    (tmp_path / "ACME").mkdir(parents=True)
    (tmp_path / "ACME" / "Report.docx").write_text("pre-existing")
    src = tmp_path / "r.md"
    src.write_text("# Report\n\nbody")
    dp = delivery.deliver_product(src, project_code="ACME", task_id="ACME-T-009")
    assert dp.dest.name == "Report (ACME-T-009).docx"  # didn't clobber


# ── code deliverables ship verbatim (not pandoc'd to docx) ───────────────

def test_code_source_detection():
    assert delivery._is_code_source(Path("a/game.py"))
    assert delivery._is_code_source(Path("a/main.js"))
    assert delivery._is_code_source(Path("a/lib.rs"))
    assert delivery._is_code_source(Path("a/Dockerfile"))  # no suffix, by name
    assert not delivery._is_code_source(Path("a/report.md"))
    assert not delivery._is_code_source(Path("a/notes.txt"))


def test_deliver_code_ships_verbatim(monkeypatch, tmp_path):
    """A code deliverable is copied byte-for-byte, keeping game.py — never
    rendered through pandoc into a Word doc. export_artifact must NOT be
    called for code (assert via a poisoned stub)."""
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))

    def _poison(*a, **k):  # pragma: no cover - must not run for code
        raise AssertionError("export_artifact called for a code deliverable")
    monkeypatch.setattr(delivery, "export_artifact", _poison)

    src = tmp_path / "game.py"
    body = "import pygame\n\n\ndef main():\n    pass\n"
    src.write_text(body)
    dp = delivery.deliver_product(src, project_code="MOD", task_id="MOD-T-003")
    assert dp.error is None
    assert dp.dest == tmp_path / "MOD" / "game.py"  # name + extension preserved
    assert dp.dest.read_text() == body  # byte-for-byte, runnable


def test_deliver_code_collision_keeps_extension(monkeypatch, tmp_path):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    (tmp_path / "MOD").mkdir(parents=True)
    (tmp_path / "MOD" / "game.py").write_text("old")
    src = tmp_path / "g.py"
    src.write_text("new source")
    src = src.rename(tmp_path / "game.py")  # same basename as the existing dest
    dp = delivery.deliver_product(src, project_code="MOD", task_id="MOD-T-003")
    assert dp.dest.name == "game (MOD-T-003).py"  # disambiguated, .py kept
    assert dp.dest.read_text() == "new source"


def test_deliver_finished_products_skips_missing(monkeypatch, tmp_path, _mock_export):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    real = tmp_path / "real.md"
    real.write_text("# Real Deliverable\n\nx")
    out = delivery.deliver_finished_products(
        [("T-1", real, None, "document"), ("T-2", tmp_path / "missing.md", None, "document")],
        project_code="P",
    )
    assert len(out) == 1  # missing one skipped
    assert out[0].name == "Real Deliverable"


# ── binary / data deliverables ship verbatim (not pandoc'd, byte-preserving) ─

def test_deliver_media_binary_ships_verbatim(monkeypatch, tmp_path):
    """A media composite (.mp4/.zip/.png) is a finished BINARY — it must be
    copied byte-for-byte, never read as text and re-rendered through pandoc
    (which corrupts it and breaks the QC binary-aware checksum guarantee).
    export_artifact must NOT be called."""
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))

    def _poison(*a, **k):  # pragma: no cover - must not run for a binary
        raise AssertionError("export_artifact called for a binary deliverable")
    monkeypatch.setattr(delivery, "export_artifact", _poison)

    src = tmp_path / "clip.mp4"
    payload = b"\x00\x00\x00\x18ftypmp42\x00\x01\xff\xfe binary bytes \x80\x90"
    src.write_bytes(payload)
    dp = delivery.deliver_product(src, project_code="MOD", task_id="MOD-T-007")
    assert dp.error is None
    assert dp.dest == tmp_path / "MOD" / "clip.mp4"  # name + extension preserved
    assert dp.dest.read_bytes() == payload  # byte-for-byte, checksum intact


def test_deliver_data_csv_ships_verbatim(monkeypatch, tmp_path):
    """A data deliverable (.csv) is product-agnostic content, NOT Markdown
    prose. It must arrive as results.csv with identical bytes — not rendered
    to a stray ``name,score.docx`` (its first row mistaken for an H1 title)."""
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))

    def _poison(*a, **k):  # pragma: no cover - must not run for data
        raise AssertionError("export_artifact called for a data deliverable")
    monkeypatch.setattr(delivery, "export_artifact", _poison)

    src = tmp_path / "results.csv"
    body = "name,score\nalice,9\nbob,7\n"
    src.write_text(body)
    dp = delivery.deliver_product(src, project_code="MOD", task_id="MOD-T-008")
    assert dp.error is None
    assert dp.dest == tmp_path / "MOD" / "results.csv"  # name + .csv kept
    assert dp.dest.read_text() == body  # byte-for-byte


def test_deliver_rendered_pdf_binary_ships_verbatim(monkeypatch, tmp_path):
    """An ALREADY-rendered .pdf (real binary bytes) ships verbatim — it is not
    Markdown text, so it must not be re-staged + re-rendered through pandoc."""
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))

    def _poison(*a, **k):  # pragma: no cover - must not run for a binary pdf
        raise AssertionError("export_artifact called for a rendered binary pdf")
    monkeypatch.setattr(delivery, "export_artifact", _poison)

    src = tmp_path / "report.pdf"
    payload = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< >>\nendobj\n"
    src.write_bytes(payload)
    dp = delivery.deliver_product(src, project_code="MOD", task_id="MOD-T-010")
    assert dp.error is None
    assert dp.dest == tmp_path / "MOD" / "report.pdf"
    assert dp.dest.read_bytes() == payload


def test_leader_named_pdf_still_markdown_text_renders(monkeypatch, tmp_path):
    """Back-compat: a deliverable the Leader NAMED ``report.pdf`` but whose
    bytes are still hand-written Markdown TEXT must still flow through the
    render path (the historical reason render ignored on-disk extension)."""
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))

    called = {}

    def _fake(md, dest, fmt):
        called["yes"] = (Path(md).read_text(), fmt)
        Path(dest).write_text("rendered")
        return ExportResult(source=Path(md), dest=Path(dest), format=fmt, error=None)
    monkeypatch.setattr(delivery, "export_artifact", _fake)

    src = tmp_path / "report.pdf"
    src.write_text("# Quarterly Brief\n\nstill markdown text")
    dp = delivery.deliver_product(src, project_code="MOD", task_id="MOD-T-011")
    assert dp.error is None
    assert called.get("yes"), "render path must run for Markdown-text content"
    assert dp.name == "Quarterly Brief"  # named from its H1


# ── deliverables_from_tasks (the wiring adapter) ─────────────────────────

class _FakeTask:
    def __init__(self, id, deliverable=False, output_path=None, description=None,
                 artifact_kind="document", required_skills=None):
        self.id = id
        self.deliverable = deliverable
        self.output_path = output_path
        self.description = description
        self.artifact_kind = artifact_kind
        self.required_skills = required_skills or []


def test_code_bundle_signals_on_family_not_just_suffix(monkeypatch, tmp_path):
    """A code bundle is detected by FAMILY too, not only suffix.

    A code-family fallback lands at `*.txt` (`_is_code_source` is false), so a
    README.md companion beside it must still ship VERBATIM as part of the
    runnable folder — never rendered to .docx."""
    from modulatio import families
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path / "out"))
    art = tmp_path / "art"
    code_task = _FakeTask("CODE-T-1", deliverable=True, output_path=None,
                          artifact_kind="code")
    readme_task = _FakeTask("README-T-1", deliverable=True,
                            output_path="README.md", artifact_kind="document")
    # writer drops the code at the family-aware fallback (drafts/code-t-1.txt)
    code_file = art / "drafts" / families.draft_fallback_name(code_task)
    code_file.parent.mkdir(parents=True, exist_ok=True)
    code_file.write_text("print('hi')\n")
    readme = art / "README.md"
    readme.write_text("# Demo\n\nRun it.\n")
    # pandoc must NOT run for EITHER file — the README is a code-bundle companion
    def _poison(*a, **k):  # pragma: no cover — must not run in a code bundle
        raise AssertionError("export_artifact called inside a code bundle")
    monkeypatch.setattr(delivery, "export_artifact", _poison)
    delivs = delivery.deliverables_from_tasks([code_task, readme_task], art)
    out = delivery.deliver_finished_products(delivs, project_code="MOD")
    names = sorted(p.dest.name for p in out)
    assert names == ["README.md", "code-t-1.txt"]      # NOT Demo.docx
    assert out[1].dest.read_text() == "# Demo\n\nRun it.\n"  # companion verbatim


def test_code_fallback_path_consistency_and_verbatim(monkeypatch, tmp_path):
    """A CODE task with NO output_path is written to the
    family-aware fallback (drafts/<id>.txt). Delivery must (1) resolve the SAME
    path so it is not silently lost, and (2) ship it VERBATIM — code is NOT
    pandoc-rendered, keyed on the FAMILY not the .txt suffix (which is globally
    classified as prose)."""
    from modulatio import families
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path / "out"))
    art = tmp_path / "art"
    task = _FakeTask("CODE-T-1", deliverable=True, output_path=None,
                     artifact_kind="code")
    written = families.draft_fallback_name(task)
    assert written == "code-t-1.txt"               # writer's family-aware path
    code_file = art / "drafts" / written
    code_file.parent.mkdir(parents=True, exist_ok=True)
    raw = "def f():\n    return {'x': [1, 2, 3]}\n"
    code_file.write_text(raw)
    # (1) HIGH: delivery resolves the SAME path + carries the family
    delivs = delivery.deliverables_from_tasks([task], art)
    assert delivs[0][1] == code_file
    assert delivs[0][3] == "code"
    # (2) MED: a code family ships VERBATIM — pandoc export must NOT run
    def _poison(*a, **k):  # pragma: no cover — must not run for code
        raise AssertionError("export_artifact called for a code deliverable")
    monkeypatch.setattr(delivery, "export_artifact", _poison)
    out = delivery.deliver_finished_products(delivs, project_code="MOD")
    assert len(out) == 1                            # delivered, not silently lost
    assert out[0].dest.read_text() == raw           # raw code preserved


def test_deliverables_from_tasks_filters_and_resolves(tmp_path):
    tasks = [
        _FakeTask("T-1", deliverable=False),                         # skipped
        _FakeTask("T-2", deliverable=True, output_path="paper.md", description="the paper"),
        _FakeTask("T-3", deliverable=True, output_path=None, description="report"),  # default path
    ]
    out = delivery.deliverables_from_tasks(tasks, tmp_path)
    assert len(out) == 2  # only the deliverables
    assert out[0] == ("T-2", tmp_path / "paper.md", "the paper", "document")
    # default drafts/<id>.md — lowercased to match the producer's writer
    assert out[1] == ("T-3", tmp_path / "drafts/t-3.md", "report", "document")


def test_deliverables_from_tasks_lowercases_default_path(tmp_path):
    """The producer writes the default draft to ``drafts/{id.lower()}.md``
    (orchestration.py:3731). deliverables_from_tasks must resolve to the SAME
    lowercase path or an uppercase-id deliverable is silently never delivered.
    An explicit output_path is honored verbatim (not lowercased)."""
    tasks = [
        _FakeTask("ABC-T-001", deliverable=True, output_path=None, description="upper id"),
        _FakeTask("XYZ-T-002", deliverable=True, output_path="Sub/Keep-Case.md", description="explicit"),
    ]
    out = delivery.deliverables_from_tasks(tasks, tmp_path)
    assert out[0] == ("ABC-T-001", tmp_path / "drafts/abc-t-001.md", "upper id", "document")
    assert out[1] == ("XYZ-T-002", tmp_path / "Sub/Keep-Case.md", "explicit", "document")


# ── real render (integration; skipped without pandoc) ────────────────────

@pytest.mark.skipif(
    shutil.which("pandoc") is None and not delivery.__dict__.get("_HAS_PYPANDOC"),
    reason="needs pandoc/pypandoc for a real docx render",
)
def test_real_docx_render(monkeypatch, tmp_path):
    from modulatio import export
    if not (export._has_pypandoc() or export._has_system_pandoc()):
        pytest.skip("no pandoc")
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    src = tmp_path / "doc.md"
    src.write_text("# Real Title\n\n## Section\n\n" + ("Long body paragraph. " * 40))
    dp = delivery.deliver_product(src, project_code="RND", task_id="RND-T-1")
    assert dp.error is None
    assert dp.dest.exists() and dp.dest.stat().st_size > 0
    assert dp.dest.suffix == ".docx"


# ── withhold guard: don't ship a product built on blocked work ───────────

class _StatusTask:
    def __init__(self, id, status):
        self.id = id
        self.status = status


def test_blocked_task_ids_flags_failed_states():
    tasks = [
        _StatusTask("T-1", "completed"),
        _StatusTask("T-2", "blocked"),
        _StatusTask("T-3", "pending"),
        _StatusTask("T-4", "qc_rejected"),
        _StatusTask("T-5", "abandoned"),
    ]
    assert delivery.blocked_task_ids(tasks) == ["T-2", "T-4", "T-5"]


def test_blocked_task_ids_handles_enum_status():
    from modulatio.types import TaskStatus
    tasks = [_StatusTask("T-1", TaskStatus.BLOCKED), _StatusTask("T-2", TaskStatus.COMPLETED)]
    assert delivery.blocked_task_ids(tasks) == ["T-1"]


def test_blocked_task_ids_empty_when_all_clean():
    assert delivery.blocked_task_ids([_StatusTask("T-1", "completed")]) == []


# ── cross-goal withhold guard (2026-05-30): a blocked GOAL withholds too ──

def test_blocked_goal_ids_flags_blocked_and_abandoned():
    """The live failure: a research goal whose plan is rejected goes BLOCKED
    with zero tasks, invisible to the task-level guard. blocked_goal_ids
    catches it so the downstream ungrounded product is withheld."""
    goals = [
        _StatusTask("G-1", "completed"),
        _StatusTask("G-2", "blocked"),       # plan-rejected research goal
        _StatusTask("G-3", "in_progress"),
        _StatusTask("G-4", "abandoned"),
    ]
    assert delivery.blocked_goal_ids(goals) == ["G-2", "G-4"]


def test_blocked_goal_ids_handles_enum_status():
    from modulatio.types import GoalStatus
    goals = [_StatusTask("G-1", GoalStatus.BLOCKED), _StatusTask("G-2", GoalStatus.COMPLETED)]
    assert delivery.blocked_goal_ids(goals) == ["G-1"]


def test_blocked_goal_ids_empty_when_all_clean():
    goals = [_StatusTask("G-1", "completed"), _StatusTask("G-2", "in_progress")]
    assert delivery.blocked_goal_ids(goals) == []


# ── Product Quality Report (2026-05-30): Leader's advisory, ships as docx ──

def test_quality_report_all_clear_when_no_reservations():
    body = delivery.build_product_quality_report([])
    assert body.startswith("# Product Quality Report")
    assert "No outstanding reservations" in body
    assert "ADVISORY" in body  # framed as non-blocking


def test_quality_report_lists_concern_and_recommended_check():
    recs = [{"goal_id": "P-G-001",
             "concern": "Citations not independently verified",
             "suggestion": "Spot-check the 12 cited URLs resolve"}]
    body = delivery.build_product_quality_report(recs)
    assert "Citations not independently verified" in body
    assert "Spot-check the 12 cited URLs resolve" in body
    assert "P-G-001" in body


def test_quality_report_delivers_as_docx(monkeypatch, tmp_path, _mock_export):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    dp = delivery.deliver_product_quality_report(
        [{"goal_id": "P-G-001", "concern": "X", "suggestion": "Y"}],
        project_code="ACME",
    )
    assert dp is not None and dp.error is None
    assert dp.dest.suffix == ".docx"             # ships as docx, always
    assert dp.dest.name == "Product Quality Report.docx"
    assert dp.dest.exists()


def test_quality_report_ships_even_with_no_reservations(monkeypatch, tmp_path, _mock_export):
    """The 'all clear' report still ships — its absence would be ambiguous."""
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    dp = delivery.deliver_product_quality_report([], project_code="ACME")
    assert dp is not None and dp.error is None and dp.dest.exists()


# ── 2b (2026-05-30): iteration delivery — dedup one file, replace not pile ──

class _DT:
    def __init__(self, tid, op):
        self.deliverable = True
        self.id = tid
        self.output_path = op
        self.description = "d"


def test_deliverables_dedup_same_path(tmp_path):
    """Iteration shape: three tasks edit one game.py → ONE deliverable, keyed
    to the last (final-state) task — not three identical copies."""
    tasks = [_DT("T-1", "game.py"), _DT("T-2", "game.py"), _DT("T-3", "game.py")]
    out = delivery.deliverables_from_tasks(tasks, tmp_path / "art")
    assert len(out) == 1
    assert out[0][0] == "T-3"  # last writer wins


def test_deliverables_keep_distinct_paths(tmp_path):
    """Distinct output paths are all kept (dedup only collapses same-path)."""
    tasks = [_DT("T-1", "game.py"), _DT("T-2", "level.py")]
    out = delivery.deliverables_from_tasks(tasks, tmp_path / "art")
    assert {o[0] for o in out} == {"T-1", "T-2"}


def test_pinned_code_replaces_prior_copy(tmp_path, monkeypatch):
    """An improved PINNED file overwrites its prior same-named copy (one clean
    game.py, latest version) instead of a disambiguated duplicate."""
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    art = tmp_path / "art"
    art.mkdir()
    (art / "game.py").write_text("JUMP=-20\n")
    (tmp_path / "MOD").mkdir(parents=True)
    (tmp_path / "MOD" / "game.py").write_text("JUMP=-12 old\n")
    dp = delivery.deliver_product(
        art / "game.py", project_code="MOD", task_id="T-3",
        pinned_names={"game.py"},
    )
    assert dp.dest.name == "game.py"          # replaced, not "game (T-3).py"
    assert dp.dest.read_text() == "JUMP=-20\n"


def test_non_pinned_code_still_disambiguates(tmp_path, monkeypatch):
    """A non-pinned code collision still disambiguates (don't clobber unrelated
    prior work)."""
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    art = tmp_path / "art"
    art.mkdir()
    (art / "util.py").write_text("new\n")
    (tmp_path / "MOD").mkdir(parents=True)
    (tmp_path / "MOD" / "util.py").write_text("old\n")
    dp = delivery.deliver_product(art / "util.py", project_code="MOD", task_id="T-9")
    assert dp.dest.name == "util (T-9).py"
    assert (tmp_path / "MOD" / "util.py").read_text() == "old\n"  # preserved


# ── README polish: markdown companions ship beside code in a bundle ──

def test_code_bundle_markdown_companion_ships_verbatim(monkeypatch, tmp_path):
    """game.py + README.md → README.md stays README.md beside the code, NOT a
    rendered .docx. export_artifact must not run for either."""
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))

    def _poison(*a, **k):  # pragma: no cover
        raise AssertionError("export_artifact called in a code bundle")
    monkeypatch.setattr(delivery, "export_artifact", _poison)

    art = tmp_path / "art"
    art.mkdir()
    (art / "game.py").write_text("import pygame\n")
    (art / "README.md").write_text("# Hollow Knight Demo\n\nRun: python game.py\n")
    out = delivery.deliver_finished_products(
        [("T-1", art / "game.py", None, "code"), ("T-2", art / "README.md", "readme", "document")],
        project_code="MOD",
    )
    names = sorted(d.dest.name for d in out)
    assert names == ["README.md", "game.py"]  # both verbatim, coherent folder
    assert (tmp_path / "MOD" / "README.md").read_text().startswith("# Hollow Knight")


def test_pure_prose_run_still_renders_docx(monkeypatch, tmp_path, _mock_export):
    """No code in the batch → markdown deliverables still render to .docx."""
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    art = tmp_path / "art"
    art.mkdir()
    (art / "paper.md").write_text("# Annual Report\n\nbody")
    out = delivery.deliver_finished_products(
        [("T-1", art / "paper.md", None, "document")], project_code="MOD",
    )
    assert out[0].dest.name == "Annual Report.docx"  # unchanged behavior


# ── Feature A: per-job output folders ────────────────────────────────────

_RUN_ID = "20260531T143000Z-ab12cd"  # YYYYMMDDTHHMMSSZ-<hex6>


def test_job_folder_name_slug_and_date():
    assert delivery.job_folder_name("Daily Philosophy", fallback="x", run_id=_RUN_ID) \
        == "Daily Philosophy 20260531"


def test_job_folder_name_uses_fallback_when_no_slug():
    assert delivery.job_folder_name(None, fallback="Q3 Revenue Brief", run_id=_RUN_ID) \
        == "Q3 Revenue Brief 20260531"


def test_job_folder_name_none_when_nothing_usable():
    # Empty slug AND empty fallback → None (caller ships flat, not "Untitled").
    assert delivery.job_folder_name("", fallback="", run_id=_RUN_ID) is None
    assert delivery.job_folder_name("   ", fallback="  .. ", run_id=_RUN_ID) is None


def test_job_folder_name_handles_missing_run_id_date():
    # No parseable date → just the slug, no trailing space.
    assert delivery.job_folder_name("Brief", fallback="x", run_id=None) == "Brief"


def test_job_dir_none_slug_is_flat_project_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    # Nothing names it → byte-identical to the flat per-project dir (back-compat).
    assert delivery.job_dir("ABC", None, run_id=_RUN_ID, fallback="") \
        == delivery.project_delivery_dir("ABC")


def test_job_dir_nests_under_project(monkeypatch, tmp_path):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    d = delivery.job_dir("ABC", "Daily Philosophy", run_id=_RUN_ID, fallback="obj")
    assert d == tmp_path / "ABC" / "Daily Philosophy 20260531"


def test_job_dir_collision_appends_run_hex(monkeypatch, tmp_path):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    # A prior run already created the same-named folder today.
    (tmp_path / "ABC" / "Daily Philosophy 20260531").mkdir(parents=True)
    d = delivery.job_dir("ABC", "Daily Philosophy", run_id=_RUN_ID, fallback="obj")
    assert d == tmp_path / "ABC" / "Daily Philosophy 20260531 (ab12cd)"


def test_job_dir_base_kwarg_overrides_project_delivery_dir(monkeypatch, tmp_path):
    """The FOLDERS output pick: a base overrides project_delivery_dir — even
    when MODULATIO_DELIVERY_DIR is also set (pick > env > default)."""
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path / "env-root"))
    picked = tmp_path / "picked"
    d = delivery.job_dir("ABC", "Daily Philosophy", run_id=_RUN_ID,
                         fallback="obj", base=picked)
    assert d == picked / "Daily Philosophy 20260531"
    # Nothing names it → the flat picked base itself.
    assert delivery.job_dir("ABC", None, run_id=_RUN_ID, fallback="",
                            base=picked) == picked


def test_job_slug_strips_unicode_bidi_controls():
    # BIDI override / isolates / NEL survive the ASCII
    # regex but scramble an `ls` listing -- strip them (slug is Leader JSON in B2).
    assert delivery._job_slug("a\u202ebc") == "abc"          # RLO override
    assert delivery._job_slug("Brief\u2066x\u2069") == "Briefx"  # isolates
    assert delivery._job_slug("line\u0085two") == "linetwo"      # C1 NEL


def test_job_dir_rejects_path_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    # An LLM-supplied traversal slug must stay inside the project's dir.
    d = delivery.job_dir("ABC", "../../etc/passwd", run_id=_RUN_ID, fallback="obj")
    assert delivery.project_delivery_dir("ABC") in d.parents
    assert ".." not in d.parts


def test_deliver_product_dest_override_nests(monkeypatch, tmp_path, _mock_export):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    job = delivery.job_dir("ACME", "My Job", run_id=_RUN_ID, fallback="obj")
    src = tmp_path / "p.md"
    src.write_text("# Annual Report 2026\n\nbody")
    dp = delivery.deliver_product(src, project_code="ACME", task_id="T-1", dest_override=job)
    assert dp.error is None
    assert dp.dest == job / "Annual Report 2026.docx"
    assert dp.dest.exists()


def test_deliver_product_dest_override_none_is_flat(monkeypatch, tmp_path, _mock_export):
    # dest_override=None → byte-identical to today's flat placement.
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    src = tmp_path / "p.md"
    src.write_text("# Report\n\nbody")
    dp = delivery.deliver_product(src, project_code="ACME", task_id="T-1", dest_override=None)
    assert dp.dest == tmp_path / "ACME" / "Report.docx"


def test_deliver_finished_products_threads_dest_override(monkeypatch, tmp_path, _mock_export):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    job = delivery.job_dir("MOD", "Sprint Output", run_id=_RUN_ID, fallback="obj")
    art = tmp_path / "art"
    art.mkdir()
    (art / "paper.md").write_text("# Brief\n\nbody")
    out = delivery.deliver_finished_products(
        [("T-1", art / "paper.md", None, "document")], project_code="MOD", dest_override=job,
    )
    assert out[0].dest == job / "Brief.docx"


# ═══ fold: test_delivery_preship.py ═══
# Regression tests for the 0.9.0 pre-ship sweep finding on delivery.py.
#
# - [MEDIUM/correctness] A genuinely-rendered .rtf deliverable was re-rendered
#   (corrupted) because RTF is text, not binary. ``_looks_binary`` flags binary
#   only on a NUL byte or non-UTF-8 decode; a real RTF (``{\rtf1\ansi...}``) is
#   pure ASCII with no NUL, so it sailed past as "prose" and got re-rendered.
#   Fix: gate declared signatured formats on their magic signature before the
#   ``_looks_binary`` fallback, so a real RTF ships verbatim while a Markdown
#   blob merely *named* ``.rtf`` still renders.


def test_real_rtf_is_not_prose(tmp_path: Path) -> None:
    """A genuinely-rendered RTF (carries the ``{\\rtf`` signature) must ship
    verbatim — NOT flow through the Markdown render path (which corrupts it)."""
    rtf = tmp_path / "report.rtf"
    rtf.write_bytes(
        b"{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Times New Roman;}}\n"
        b"\\f0\\fs24 Hello, this is a real rendered RTF document.\\par\n}"
    )
    assert _is_prose_source(rtf) is False


def test_markdown_named_rtf_is_still_prose(tmp_path: Path) -> None:
    """A Markdown blob the Leader merely *named* ``.rtf`` lacks the ``{\\rtf``
    signature, so it must still render (remains prose)."""
    fake = tmp_path / "draft.rtf"
    fake.write_text("# My Report\n\nThis is hand-written **Markdown**, not RTF.\n")
    assert _is_prose_source(fake) is True


def test_real_pdf_is_not_prose(tmp_path: Path) -> None:
    """An already-rendered binary PDF still ships verbatim (unchanged behavior)."""
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n")
    assert _is_prose_source(pdf) is False


def test_markdown_named_pdf_is_still_prose(tmp_path: Path) -> None:
    """A Markdown blob named ``.pdf`` (no ``%PDF-`` signature) still renders."""
    fake = tmp_path / "draft.pdf"
    fake.write_text("# Title\n\nMarkdown body that should be rendered to a real PDF.\n")
    assert _is_prose_source(fake) is True


def test_plain_markdown_is_prose(tmp_path: Path) -> None:
    """A plain ``.md`` deliverable stays prose (no signatured extension)."""
    md = tmp_path / "notes.md"
    md.write_text("# Notes\n\nSome content.\n")
    assert _is_prose_source(md) is True


# ═══ fold: test_delivery_r2_audit.py ═══
# Regression tests for the r2 debug findings on delivery._looks_binary.
#
# Findings:
#   - resource-leak: _looks_binary leaked a file descriptor (open() not closed).
#   - edge-case: a fixed-size 8192-byte read can split a multibyte UTF-8 code
#     point at the boundary, mis-classifying valid text as binary.


def test_looks_binary_does_not_leak_file_descriptor(tmp_path: Path) -> None:
    """The read must happen inside a context manager so no fd is left open
    (and no ResourceWarning is emitted for an unclosed file)."""
    p = tmp_path / "plain.md"
    p.write_text("# hello\nsome text\n", encoding="utf-8")

    gc.collect()
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        # Repeated calls would accumulate leaked fds before the fix; under the
        # ResourceWarning-as-error filter an unclosed file surfaces here.
        for _ in range(50):
            assert _looks_binary(p) is False
        gc.collect()


def test_looks_binary_utf8_split_at_boundary_is_text(tmp_path: Path) -> None:
    """A valid UTF-8 file whose 8192-byte head ends mid-multibyte-char must
    NOT be classified as binary."""
    # Fill so that the 4-byte emoji straddles the 8192-byte read boundary.
    emoji = "\U0001f600"  # 4 UTF-8 bytes
    encoded_emoji = emoji.encode("utf-8")
    assert len(encoded_emoji) == 4
    # Pad with ASCII so the emoji begins at byte 8190 -> only 2 of its 4 bytes
    # land inside the 8192-byte head read.
    padding = "a" * 8190
    content = (padding + emoji + "more valid text after").encode("utf-8")
    p = tmp_path / "wide.md"
    p.write_bytes(content)

    # Sanity: the raw head genuinely fails a naive strict decode (proving the
    # boundary truncation is real).
    head = content[:8192]
    naive_failed = False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        naive_failed = True
    assert naive_failed, "test setup must straddle the boundary"

    assert _looks_binary(p) is False


def test_looks_binary_real_binary_still_detected(tmp_path: Path) -> None:
    """Genuine binary content (NUL byte and/or invalid UTF-8 mid-stream) is
    still classified as binary."""
    nul = tmp_path / "nul.bin"
    nul.write_bytes(b"PK\x03\x04\x00\x00 rest of a zip")
    assert _looks_binary(nul) is True

    invalid = tmp_path / "invalid.bin"
    # Invalid UTF-8 in the MIDDLE of the head (not a truncated tail).
    invalid.write_bytes(b"text \xff\xfe more text" + b"x" * 100)
    assert _looks_binary(invalid) is True


def test_looks_binary_plain_text_false(tmp_path: Path) -> None:
    p = tmp_path / "plain.txt"
    p.write_text("just normal prose, nothing binary here\n", encoding="utf-8")
    assert _looks_binary(p) is False


def test_looks_binary_missing_file_false(tmp_path: Path) -> None:
    assert _looks_binary(tmp_path / "nope.md") is False


def test_name_rejects_every_rule_style_not_just_dashes():
    """Any line that only draws a rule must fall back, whatever it is drawn
    with. A banner of equals signs across the top of a body reached the
    delivery folder as a fifty-character filename the operator could not
    identify, and a second such deliverable would collide with it."""
    for rule in ("=" * 50, "***", "~~~~~~", "###", "_____", ". . ."):
        assert delivery.human_name_from_markdown(
            f"{rule}\nTask Report\n\nbody", fallback="Taskwatch Report"
        ) == "Taskwatch Report", f"{rule!r} was accepted as a title"


def test_name_keeps_a_title_that_merely_contains_rule_characters():
    """Only a line made ENTIRELY of rule characters is a rule; the characters
    are ordinary inside a real title and must not cost the document its name."""
    assert delivery.human_name_from_markdown(
        "Cost-Benefit Analysis\n\nbody", fallback="fb"
    ) == "Cost-Benefit Analysis"
    # A setext heading keeps its TITLE — the underline is the following line.
    assert delivery.human_name_from_markdown(
        "Task Report\n===========\n\nbody", fallback="fb"
    ) == "Task Report"


def test_a_product_is_named_for_the_work_not_the_task():
    """A task id names nothing a reader recognises. The delivery FOLDER is
    already named from the job or the objective; a file falling back to an
    identifier is the same product named two different ways."""
    from modulatio import delivery

    # A usable title always wins.
    assert delivery.human_name_from_markdown(
        "# Quarterly Report\n\nbody", fallback="apppkg") == "Quarterly Report"
    # With no usable title, the work's own name carries it.
    assert delivery.human_name_from_markdown(
        "---\n\nbody", fallback="apppkg") == "apppkg"


def test_a_brief_used_as_a_name_is_bounded_like_a_folder_name():
    """A brief is a paragraph. Sanitizing one whole produces a filename cut
    mid-word that names nothing, so a fallback is bounded the way the delivery
    folder's name is bounded."""
    from modulatio import delivery

    brief = (
        "Build apppkg, an installable Python package that reports on a TODO "
        "list stored as JSON. Package layout: three modules under apppkg/, "
        "each with its own responsibility and no circular dependencies."
    )
    named = delivery.human_name_from_markdown("---\n\nbody", fallback=brief)
    assert len(named) <= 64, named
    assert named.startswith("Build apppkg")
    # Cut at a word edge: the last word of the name is a whole word of the
    # brief, not a fragment of one.
    assert named.split()[-1] in brief.split()
