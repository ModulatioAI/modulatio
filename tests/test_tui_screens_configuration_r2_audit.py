"""Regression tests for the configuration TUI screen (round-2 audit).

Covers the `register()` error-handling defect: a ValueError raised by
``model_presets.add_preset`` for a *validation/security* reason (bad
api_format/auth_type, or the secret-leak keel) was being swallowed and
re-interpreted as "already registered -> update in place", masking a genuine
rejection and routing it onto the update path (which skips the secret keel).

The fix: only re-route to ``update_preset`` when the entry actually exists;
otherwise re-raise so the rejection surfaces.
"""

from __future__ import annotations

import json

import pytest

from modulatio import model_presets
from modulatio.tui.screens.configuration import ConfigScreen


def _make_screen():
    """Build a ConfigScreen without the Textual app/mount machinery.

    `register()` only reads plain instance attributes + calls into
    model_presets / provider_catalog, so a bare instance with the relevant
    attrs set is enough to exercise it in isolation.
    """
    screen = object.__new__(ConfigScreen)
    # A real catalog provider with a standard api-key auth path.
    screen._provider_id = "openai"
    screen._auth_type = "api_key"
    screen._env_var = "OPENAI_API_KEY"
    screen._base_url = None
    screen._pool = False
    return screen


@pytest.fixture()
def isolated_presets(tmp_path, monkeypatch):
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "model_presets.json")
    return tmp_path


def test_register_reraises_validation_error_for_nonexistent_key(
    isolated_presets, monkeypatch
):
    """A validation/security ValueError on a key that does NOT exist must
    surface, not be masked as a successful in-place update."""
    screen = _make_screen()

    update_calls = []

    def fake_add_preset(key, **kwargs):
        # Simulate the secret-leak keel / format-validation rejection: the key
        # does not yet exist, so this is a genuine rejection.
        raise ValueError("auth_config may not carry raw secret value(s)")

    def fake_update_preset(key, **kwargs):
        update_calls.append(key)
        return {}

    monkeypatch.setattr(model_presets, "add_preset", fake_add_preset)
    monkeypatch.setattr(model_presets, "update_preset", fake_update_preset)

    with pytest.raises(ValueError, match="raw secret"):
        screen.register("gpt-4o")

    # Crucially: the rejection was NOT silently re-routed to update_preset.
    assert update_calls == []


def test_register_routes_to_update_when_key_already_exists(isolated_presets):
    """When the entry genuinely already exists, register() still updates it in
    place (the intended pre-existing behavior is preserved)."""
    screen = _make_screen()

    # First registration creates the entry on disk.
    key = screen.register("gpt-4o")
    assert key is not None
    assert model_presets.get_preset(key) is not None
    first = model_presets.get_preset(key)

    # Second registration hits the "already exists" ValueError -> update in
    # place. It must succeed (no raise) and the entry must remain present.
    key2 = screen.register("gpt-4o")
    assert key2 == key
    assert model_presets.get_preset(key) is not None
    # Sanity: the label-bearing add path and the label-stripped update path both
    # leave a coherent entry.
    second = model_presets.get_preset(key)
    assert second["model"] == first["model"]


def test_register_does_not_create_entry_when_add_rejected(
    isolated_presets, monkeypatch
):
    """A rejected registration must not leave any preset persisted on disk."""
    screen = _make_screen()

    def fake_add_preset(key, **kwargs):
        raise ValueError("api_format must be one of ...")

    monkeypatch.setattr(model_presets, "add_preset", fake_add_preset)

    with pytest.raises(ValueError):
        screen.register("gpt-4o")

    presets_file = isolated_presets / "model_presets.json"
    if presets_file.exists():
        assert json.loads(presets_file.read_text()) == {}
