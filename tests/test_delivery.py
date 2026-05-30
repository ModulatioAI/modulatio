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


def test_deliver_product_collision_disambiguates(monkeypatch, tmp_path, _mock_export):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    (tmp_path / "ACME").mkdir(parents=True)
    (tmp_path / "ACME" / "Report.docx").write_text("pre-existing")
    src = tmp_path / "r.md"
    src.write_text("# Report\n\nbody")
    dp = delivery.deliver_product(src, project_code="ACME", task_id="ACME-T-009")
    assert dp.dest.name == "Report (ACME-T-009).docx"  # didn't clobber


def test_deliver_finished_products_skips_missing(monkeypatch, tmp_path, _mock_export):
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(tmp_path))
    real = tmp_path / "real.md"
    real.write_text("# Real Deliverable\n\nx")
    out = delivery.deliver_finished_products(
        [("T-1", real, None), ("T-2", tmp_path / "missing.md", None)],
        project_code="P",
    )
    assert len(out) == 1  # missing one skipped
    assert out[0].name == "Real Deliverable"


# ── deliverables_from_tasks (the wiring adapter) ─────────────────────────

class _FakeTask:
    def __init__(self, id, deliverable=False, output_path=None, description=None):
        self.id = id
        self.deliverable = deliverable
        self.output_path = output_path
        self.description = description


def test_deliverables_from_tasks_filters_and_resolves(tmp_path):
    tasks = [
        _FakeTask("T-1", deliverable=False),                         # skipped
        _FakeTask("T-2", deliverable=True, output_path="paper.md", description="the paper"),
        _FakeTask("T-3", deliverable=True, output_path=None, description="report"),  # default path
    ]
    out = delivery.deliverables_from_tasks(tasks, tmp_path)
    assert len(out) == 2  # only the deliverables
    assert out[0] == ("T-2", tmp_path / "paper.md", "the paper")
    assert out[1] == ("T-3", tmp_path / "drafts/T-3.md", "report")  # default drafts/<id>.md


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
