# SPDX-License-Identifier: Apache-2.0
"""app.py wiring for the two-lane Leader widen: the converse worker supplies the
gate prompt_fn, and the /rp + /work side-effects reach the gate.

These test the GLUE (the gate/modal/bridge are covered elsewhere): that
``_apply_side_effect`` routes the two Leader side-effects, that ``/rp`` calls
``revoke_all`` on the conversation orchestrator's gate, and that the converse
worker hands ``orch.converse`` a ``prompt_fn``.
"""
from __future__ import annotations

import pytest

from modulatio import config, setup_state, vault
from modulatio.tui.app import ModulatioApp

PROJECT_CODE = "WIDEN"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE", cfg_dir / "setup-state.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


class _SpyGate:
    def __init__(self):
        self.revoked = False

    def revoke_all(self):
        self.revoked = True

    def refusal_reason(self, request):
        return None  # stub gate allows everything (the refusal logic is tested in test_leader_gate)

    def decide(self, request, *, prompt_fn):
        import modulatio.leader_gate as lg
        return lg.ScopedDecision(scope=prompt_fn(request).scope)


class _SpyOrch:
    def __init__(self):
        self._gate = _SpyGate()
        self.converse_kwargs = None

    def leader_gate(self):
        return self._gate

    def converse(self, text, **kwargs):
        self.converse_kwargs = kwargs
        return "ok"


def test_rp_side_effect_revokes_gate(monkeypatch):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    spy = _SpyOrch()
    app._conv_orch = spy
    # swallow the response render (no mounted widgets without a running app)
    monkeypatch.setattr(app, "_set_response", lambda *a, **k: None)
    app._apply_side_effect("leader_revoke_permissions")
    assert spy._gate.revoked is True


def test_rp_side_effect_safe_with_no_orchestrator(monkeypatch):
    """`/rp` before any conversation must not crash (nothing to revoke)."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    monkeypatch.setattr(app, "_set_response", lambda *a, **k: None)
    app._apply_side_effect("leader_revoke_permissions")  # must not raise


def test_work_here_missing_path_is_reported(monkeypatch):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    spy = _SpyOrch()
    app._conv_orch = spy
    out = {}
    monkeypatch.setattr(app, "_set_response", lambda msg, *a, **k: out.__setitem__("msg", msg))
    # never reach push_screen for a non-existent path
    monkeypatch.setattr(app, "push_screen", lambda *a, **k: pytest.fail("should not prompt"))
    app._apply_side_effect("leader_work_here:/no/such/folder/xyz")
    assert "No such folder" in out["msg"]


def test_work_here_existing_path_prompts(tmp_path, monkeypatch):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    spy = _SpyOrch()
    app._conv_orch = spy
    monkeypatch.setattr(app, "_set_response", lambda *a, **k: None)
    pushed = {}

    def fake_push(screen, *a, **k):
        pushed["screen"] = screen

    monkeypatch.setattr(app, "push_screen", fake_push)
    target = tmp_path / "realproj"
    target.mkdir()
    app._apply_side_effect(f"leader_work_here:{target}")
    from modulatio.tui.widgets.leader_approval_modal import LeaderApprovalModal
    assert isinstance(pushed["screen"], LeaderApprovalModal)


def test_work_here_refused_root_does_not_prompt(tmp_path, monkeypatch):
    """A /work target overlapping a deliverable tree is engine-refused — the
    operator sees the reason and is NOT shown a (pointless) approval modal."""
    from modulatio import leader_gate as lg

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    target = tmp_path / "proj"
    deliv = target / "runs" / "r1"
    deliv.mkdir(parents=True)

    class _GateOrch:
        def __init__(self):
            self._gate = lg.LeaderPermissionGate(
                PROJECT_CODE, workspace=tmp_path / "ws",
                blocked_subtrees=[str(deliv)],
            )

        def leader_gate(self):
            return self._gate

    monkeypatch.setattr(app, "_conversation_orchestrator", lambda: _GateOrch())
    out = {}
    monkeypatch.setattr(app, "_set_response", lambda msg, *a, **k: out.__setitem__("msg", msg))
    monkeypatch.setattr(app, "push_screen", lambda *a, **k: pytest.fail("must not prompt for a refused root"))
    app._apply_side_effect(f"leader_work_here:{target}")
    assert "deliverable" in out["msg"].lower() or "overlaps" in out["msg"].lower()


def test_run_converse_passes_prompt_fn():
    """The converse body (extracted from the @work worker) hands converse a
    prompt_fn so the gate has a UI surface."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    spy = _SpyOrch()
    app._run_converse(spy, "hello", [])
    assert "prompt_fn" in spy.converse_kwargs
    assert callable(spy.converse_kwargs["prompt_fn"])
