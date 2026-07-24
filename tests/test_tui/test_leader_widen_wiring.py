# SPDX-License-Identifier: Apache-2.0
"""app.py wiring for the two-lane Leader widen: the converse worker supplies the
gate prompt_fn, and the /rp + /work side-effects reach the gate.

These test the GLUE (the gate/modal/bridge are covered elsewhere): that
``_apply_side_effect`` routes the two Leader side-effects, that ``/rp`` calls
the engine's all-authority revocation seam and renders its result verbatim,
and that the converse worker hands ``orch.converse`` a ``prompt_fn``.
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
    def __init__(self, revoke_result=None):
        self._gate = _SpyGate()
        self.converse_kwargs = None
        #: The engine's public revocation seam — the surface calls THIS,
        #: not private transaction/gate internals.
        self._revoke_result = revoke_result

    def leader_gate(self):
        return self._gate

    def revoke_leader_permissions(self):
        if self._revoke_result is not None:
            return self._revoke_result
        self._gate.revoke_all()
        return True, "All Leader permissions revoked — back to the workspace floor."

    def converse(self, text, **kwargs):
        self.converse_kwargs = kwargs
        return "ok"


def test_rp_side_effect_revokes_gate(monkeypatch):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    spy = _SpyOrch()
    app._conv_orch = spy
    shown: list = []
    monkeypatch.setattr(app, "_set_response", lambda msg, *a, **k: shown.append(msg))
    app._apply_side_effect("leader_revoke_permissions")
    assert spy._gate.revoked is True
    assert shown and "revoked" in shown[0]


def test_rp_side_effect_reports_a_failed_revoke(monkeypatch):
    """A revoke the engine could NOT complete renders its reason — the
    surface never prints unconditional success."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    app._conv_orch = _SpyOrch(
        revoke_result=(False, "Revoke did NOT complete. journal unreadable"))
    shown: list = []
    monkeypatch.setattr(app, "_set_response", lambda msg, *a, **k: shown.append(msg))
    app._apply_side_effect("leader_revoke_permissions")
    assert shown and "did NOT complete" in shown[0]


def test_rp_side_effect_safe_with_no_orchestrator(monkeypatch):
    """`/rp` before any conversation revokes the PROJECT's durable
    authority — grants outlive the process that made them — so the surface
    reports the service's result, never a no-target claim."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    shown: list = []
    monkeypatch.setattr(app, "_set_response", lambda msg, *a, **k: shown.append(msg))
    app._apply_side_effect("leader_revoke_permissions")  # must not raise
    assert shown and "revoked" in shown[0].lower()


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


def test_run_converse_passes_capability_ask_over_the_same_modal():
    """Converse also gets ask= — the broker's
    capability surface adapted over the SAME approval modal bridge, so
    default/goal modes ask for shell/network in the TUI instead of goal
    denying without a prompt . One approval UI, both axes."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    spy = _SpyOrch()
    app._run_converse(spy, "hello", [])
    assert callable(spy.converse_kwargs.get("ask"))
