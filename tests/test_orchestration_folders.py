"""FOLDERS wiring: registered operator folders reach EVERY seat.

The registry (config.folder_grant_roots) partitions rw vs read roots; the
orchestrator unions them into the Clay seat context and every in-process
tool registry. Two standing contracts pinned here:

- ro/output folders are readable but never editable/shell-writable;
- access to a registered folder NEVER fires a permission prompt — the
  operator already decided at configuration time (the FOLDERS tab IS the
  permission decision).
"""

from __future__ import annotations

import pytest

from modulatio import config


@pytest.fixture()
def orch(tmp_path, monkeypatch):
    from modulatio import vault
    from modulatio.orchestration import Orchestrator
    from modulatio.types import Project

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    config.reload()
    code = "FLD"
    vault.init_project(code, "folders", "obj")
    project = Project(code=code, name="folders", objective="obj",
                      leader_model="stub",
                      wiki_path=str(tmp_path / "vault" / code.lower()))
    yield Orchestrator(project, {"leader": lambda p: "ok"})
    config.reload()


def _register(tmp_path, name, mode):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    folders = config.list_folders()
    folders.append({"name": name, "path": str(d), "mode": mode, "kind": "path"})
    config.save_folders(folders)
    return d


def test_seat_context_routes_folder_modes(orch, tmp_path):
    """rw folders join the Clay grants (writable binds); ro/output folders ride
    read_only_roots (--ro-bind) — for every Clay seat, via the one seam."""
    from modulatio import claude_cli

    live = _register(tmp_path, "live", "rw")
    docs = _register(tmp_path, "docs", "ro")
    drop = _register(tmp_path, "drop", "output")

    with orch._seat_context():
        ws, grants, ro = claude_cli.current_seat_context()
    assert str(live) in grants
    assert str(live) not in ro
    assert {str(docs), str(drop)} <= set(ro)
    assert not ({str(docs), str(drop)} & set(grants))


def test_staging_registry_folder_access(orch, tmp_path):
    """Concurrent-path producers + QC: read works in ro folders, edit only in
    rw folders, and run_shell file-args reach only rw folders."""
    docs = _register(tmp_path, "docs", "ro")
    live = _register(tmp_path, "live", "rw")
    (docs / "a.txt").write_text("alpha", encoding="utf-8")
    (live / "b.txt").write_text("beta", encoding="utf-8")

    staging = tmp_path / "staging"
    staging.mkdir()
    reg = orch._staging_tool_registry(staging)

    assert "alpha" in reg["read_file"].call(path=str(docs / "a.txt"))
    assert "beta" in reg["read_file"].call(path=str(live / "b.txt"))
    with pytest.raises(ValueError):
        reg["edit_file"].call(path=str(docs / "a.txt"), old="alpha", new="x")
    reg["edit_file"].call(path=str(live / "b.txt"), old="beta", new="gamma")
    assert (live / "b.txt").read_text(encoding="utf-8") == "gamma"


def test_leader_registries_reach_folders(orch, tmp_path):
    """The solo-coding Leader unions folders with its gate grants; the
    goal-verify registry reads them too (read-class only)."""
    docs = _register(tmp_path, "docs", "ro")
    (docs / "a.txt").write_text("alpha", encoding="utf-8")

    reg = orch._leader_tool_registry()
    assert "alpha" in reg["read_file"].call(path=str(docs / "a.txt"))
    with pytest.raises(ValueError):
        reg["edit_file"].call(path=str(docs / "a.txt"), old="alpha", new="x")

    vreg = orch._leader_verify_tool_registry()
    assert "alpha" in vreg["read_file"].call(path=str(docs / "a.txt"))


def test_registered_folder_access_never_prompts(orch, tmp_path):
    """Clif's rule: the FOLDERS tab IS the permission decision. Reading a
    registered folder must never route through the widen-gate's operator
    prompt — zero SecurityRequest decisions fired."""
    docs = _register(tmp_path, "docs", "ro")
    (docs / "a.txt").write_text("alpha", encoding="utf-8")
    decisions: list = []
    gate = orch.leader_gate()
    real_decide = gate.decide
    gate.decide = lambda *a, **k: decisions.append(a) or real_decide(*a, **k)

    reg = orch._leader_tool_registry()
    assert "alpha" in reg["read_file"].call(path=str(docs / "a.txt"))
    assert decisions == []


def test_format_registered_folders_renders_and_empties(orch, tmp_path):
    """The prompt block: one line per folder — name, plain-language mode,
    path, top-level entry count — plus the usage hint. Empty registry → ""
    (the block simply doesn't appear)."""
    from modulatio.orchestration import _format_registered_folders

    assert _format_registered_folders() == ""
    docs = _register(tmp_path, "docs", "ro")
    (docs / "a.txt").write_text("x", encoding="utf-8")
    live = _register(tmp_path, "live", "rw")
    _register(tmp_path, "drop", "output")

    block = _format_registered_folders()
    assert "docs" in block and str(docs) in block
    assert "read-only" in block
    assert "read-write" in block
    assert "output" in block
    assert "read_file" in block          # the usage hint
    assert str(live) in block


def test_planner_decompose_prompt_carries_folders_block(orch, tmp_path):
    docs = _register(tmp_path, "docs", "ro")
    seen: dict = {}

    def _leader(prompt: str) -> str:
        seen["prompt"] = prompt
        return '```json\n[]\n```'

    orch.runners["leader"] = _leader
    orch._leader_decompose("objective")
    assert str(docs) in seen["prompt"]
    assert "read-only" in seen["prompt"]


def test_producer_prompt_carries_folders_block(orch, tmp_path):
    import json as _json

    docs = _register(tmp_path, "docs", "ro")
    prompts: list[str] = []

    def _leader(prompt: str) -> str:
        if "LEADER GOAL VERIFICATION" in prompt:
            return ('```json\n{"verdict": "satisfied", "rationale": "ok", '
                    '"report_body": "r"}\n```')
        return ('```json\n[{"description": "one thing", "success_criteria": '
                '"a file", "evidence_required": [{"kind": "artifact", '
                '"description": "f"}]}]\n```')

    def _planner(prompt: str) -> str:
        tasks = [{"description": "Draft it", "assignee_specialist": "drafter",
                  "evidence_required": [{"kind": "artifact",
                                         "description": "file"}]}]
        return f"```json\n{_json.dumps(tasks)}\n```"

    def _drafter(prompt: str) -> str:
        prompts.append(prompt)
        return "A draft body long enough to count as real work output here."

    def _qc(prompt: str) -> str:
        return ('```json\n{"check": "ok", "passed": true, "notes": "", '
                '"defect_type": null}\n```')

    _seed_producers(orch.project.code)
    orch.runners.update(
        {"leader": _leader, "planner": _planner, "drafter": _drafter,
         "qc": _qc})
    orch.kickoff("Draft one thing")
    assert prompts, "drafter never ran"
    assert any(str(docs) in p and "read-only" in p for p in prompts)


def test_converse_prompt_carries_folders_block(orch, tmp_path):
    docs = _register(tmp_path, "docs", "ro")
    prompt = orch._build_converse_prompt([], "hello")
    assert str(docs) in prompt


def _seed_producers(code: str, n: int = 2) -> None:
    from modulatio import roster

    for i in range(n):
        roster.save(
            roster.Agent(id=f"prod-{i}", name=f"prod-{i}",
                         identity=f"prod-{i} id", model="stub",
                         tier="producer", capacity_cap=1),
            code,
        )


def _stub_runners(prompts_sink=None):
    import json as _json

    def _leader(prompt: str) -> str:
        if "LEADER GOAL VERIFICATION" in prompt:
            return ('```json\n{"verdict": "satisfied", "rationale": "ok", '
                    '"report_body": "r"}\n```')
        return ('```json\n[{"description": "one thing", "success_criteria": '
                '"a file", "evidence_required": [{"kind": "artifact", '
                '"description": "f"}]}]\n```')

    def _planner(prompt: str) -> str:
        tasks = [{"description": "Draft it", "assignee_specialist": "drafter",
                  "deliverable": True, "output_path": "one-thing.md",
                  "evidence_required": [{"kind": "artifact",
                                         "description": "file"}]}]
        return f"```json\n{_json.dumps(tasks)}\n```"

    def _drafter(prompt: str) -> str:
        if prompts_sink is not None:
            prompts_sink.append(prompt)
        return "A draft body long enough to count as real work output here."

    def _qc(prompt: str) -> str:
        return ('```json\n{"check": "ok", "passed": true, "notes": "", '
                '"defect_type": null}\n```')

    return {"leader": _leader, "planner": _planner, "drafter": _drafter,
            "qc": _qc}


def test_delivery_lands_in_the_picked_output_folder(orch, tmp_path):
    """End-to-end: a picked output-mode folder receives the finished product
    (pick > MODULATIO_DELIVERY_DIR > ~/Documents/Modulatio)."""
    drop = _register(tmp_path, "drop", "output")
    config.set_job_output_folder("drop")
    _seed_producers(orch.project.code)
    orch.runners.update(_stub_runners())
    orch._deliver_products = True   # the real run paths construct with it on

    orch.kickoff("Draft one thing")

    delivered = list(drop.rglob("*"))
    assert any(p.is_file() for p in delivered), f"nothing landed in {drop}"


def test_picked_output_base_refuses_dangerous_root(orch, tmp_path):
    """A hand-edited output pick inside the vault (or any
    refused tree) must NOT be delivered into — the pick runs the SAME floor as
    seat grants, falling back to the default location with a summary note."""
    from modulatio.orchestration import RunSummary

    inside_vault = tmp_path / "vault" / "fld" / "unsafe-output"
    inside_vault.mkdir(parents=True)
    config.save_folders([{"name": "badout", "path": str(inside_vault),
                          "mode": "output", "kind": "path"}])
    config.set_job_output_folder("badout")

    summary = RunSummary(project=orch.project)
    assert orch._picked_output_base(summary) is None
    assert any("badout" in e for e in summary.errors)


def test_unreachable_pick_falls_back_with_note(orch, tmp_path, monkeypatch):
    """A picked folder that vanished delivers to the default location and
    says so in summary.errors — the run never raises."""
    drop = _register(tmp_path, "drop", "output")
    config.set_job_output_folder("drop")
    import shutil

    shutil.rmtree(drop)
    _seed_producers(orch.project.code)
    orch.runners.update(_stub_runners())
    orch._deliver_products = True

    summary = orch.kickoff("Draft one thing")
    assert any("drop" in e and "unreachable" in e for e in summary.errors)


def test_unreachable_folder_absent_from_all_grants(orch, tmp_path, monkeypatch):
    docs = _register(tmp_path, "docs", "ro")
    (docs / "a.txt").write_text("alpha", encoding="utf-8")
    monkeypatch.setattr(config, "probe_folder", lambda *a, **k: False)

    from modulatio import claude_cli
    with orch._seat_context():
        _ws, grants, ro = claude_cli.current_seat_context()
    assert str(docs) not in grants and str(docs) not in ro
    reg = orch._staging_tool_registry(tmp_path / "st2")
    with pytest.raises(ValueError):
        reg["read_file"].call(path=str(docs / "a.txt"))
