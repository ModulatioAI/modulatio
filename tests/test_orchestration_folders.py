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
