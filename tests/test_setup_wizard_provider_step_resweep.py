# SPDX-License-Identifier: Apache-2.0
"""0.9.0 pre-ship re-sweep regressions for setup_wizard/provider_step.py.

Finding 1 [LOW/error-path] (provider_step.run @ ~577): the configured-models
list rendered each preset with ``p.get('label')[:30]`` / ``p.get('model','?')[:24]``
straight off ``model_presets.load_presets()``, which returns the on-disk JSON
verbatim with no per-preset shape check. A hand-edited/corrupt presets file with
a NON-DICT preset value makes ``p.get`` raise AttributeError; a preset with
``model: null`` makes ``None[:24]`` raise TypeError. The render must skip
non-dict presets and coerce sliced fields to str.

Finding 2 [LOW/correctness] (_remove_flow @ ~551): removing an api_key model
left its staged env var in ``state['staged_api_keys']`` — finalize.commit then
wrote that orphaned key to <vault>/.env and listed it as a phantom provider on
the confirm screen. _remove_flow must drop the orphaned key when no surviving
model references that env var (keeping shared keys).
"""

from __future__ import annotations

import pytest

from modulatio import model_presets
from modulatio.setup_wizard import provider_step


# --- shared fixture: redirect presets persistence to a temp file --------------

@pytest.fixture
def temp_presets(tmp_path, monkeypatch):
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "model_presets.json")
    return model_presets.PRESETS_FILE


# === Finding 1: render is robust to a corrupt presets file ===================
#
# These drive the REAL provider_step.run() so a regression in the actual render
# loop is caught. run() renders the configured-models list, then prompts via
# input(); we feed "q" so it returns immediately AFTER rendering. If the render
# raises on corrupt data, the test fails before reaching the quit.

def _write_presets_json(path, obj):
    import json

    path.write_text(json.dumps(obj), encoding="utf-8")


def test_run_renders_non_dict_preset_without_crashing(temp_presets, monkeypatch):
    """A non-dict preset value (corrupt JSON) must be skipped, not crash run()."""
    _write_presets_json(
        temp_presets,
        {
            "good": {"label": "Good", "api_format": "openai", "model": "gpt-x"},
            "broken": "this-should-be-a-dict",  # corrupt: non-dict value
        },
    )
    monkeypatch.setattr("builtins.input", lambda *a, **k: "q")
    from modulatio.setup_wizard import steps

    # Must not raise AttributeError on the non-dict preset; returns QUIT.
    assert provider_step.run({}) == steps.QUIT


def test_run_renders_null_model_without_crashing(temp_presets, monkeypatch):
    """A preset with model=null (None) must not raise None[:24] in run()."""
    _write_presets_json(
        temp_presets,
        {"x": {"label": "X", "api_format": "openai", "model": None}},
    )
    monkeypatch.setattr("builtins.input", lambda *a, **k: "q")
    from modulatio.setup_wizard import steps

    assert provider_step.run({}) == steps.QUIT


# === Finding 2: removing an api_key model unstages its orphaned key ==========

def test_remove_flow_drops_orphaned_staged_key(temp_presets, monkeypatch):
    """Add an api_key model staging FOO_API_KEY, remove it → key is unstaged."""
    model_presets.add_preset(
        "foo_model",
        label="Foo",
        base_url="https://api.foo.com/v1",
        api_format="openai",
        auth_type="api_key",
        auth_config={"env_var": "FOO_API_KEY"},
        model="foo-1",
    )
    staged = {"FOO_API_KEY": "secret-value"}

    # Drive _remove_flow non-interactively: pick the model, confirm yes.
    from modulatio.setup_wizard import steps

    monkeypatch.setattr(steps, "pick_option", lambda *a, **k: "foo_model")
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: True)

    provider_step._remove_flow(staged)

    assert "FOO_API_KEY" not in staged
    assert model_presets.get_preset("foo_model") is None


def test_remove_flow_keeps_shared_staged_key(temp_presets, monkeypatch):
    """If two models share the env var, removing one keeps the key staged."""
    for key in ("a_model", "b_model"):
        model_presets.add_preset(
            key,
            label=key,
            base_url="https://api.shared.com/v1",
            api_format="openai",
            auth_type="api_key",
            auth_config={"env_var": "SHARED_API_KEY"},
            model=key,
        )
    staged = {"SHARED_API_KEY": "secret"}

    from modulatio.setup_wizard import steps

    monkeypatch.setattr(steps, "pick_option", lambda *a, **k: "a_model")
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: True)

    provider_step._remove_flow(staged)

    # b_model still references SHARED_API_KEY → key must remain staged.
    assert staged.get("SHARED_API_KEY") == "secret"
    assert model_presets.get_preset("a_model") is None
    assert model_presets.get_preset("b_model") is not None


def test_remove_flow_without_staged_keys_is_backcompat(temp_presets, monkeypatch):
    """Calling _remove_flow() with no staged_keys arg still works (back-compat)."""
    model_presets.add_preset(
        "z_model",
        label="Z",
        base_url="https://api.z.com/v1",
        api_format="openai",
        auth_type="none",
        auth_config={},
        model="z-1",
    )
    from modulatio.setup_wizard import steps

    monkeypatch.setattr(steps, "pick_option", lambda *a, **k: "z_model")
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: True)

    provider_step._remove_flow()  # no arg
    assert model_presets.get_preset("z_model") is None


def test_drop_orphaned_helper_keeps_unrelated_keys(temp_presets):
    """The helper only removes the exact orphaned env var, never others."""
    staged = {"FOO_API_KEY": "f", "BAR_API_KEY": "b"}
    removed = {"auth_config": {"env_var": "FOO_API_KEY"}, "auth_type": "api_key"}
    provider_step._drop_orphaned_staged_key("foo", removed, {}, staged)
    assert "FOO_API_KEY" not in staged
    assert staged["BAR_API_KEY"] == "b"
