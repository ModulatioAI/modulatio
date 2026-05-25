"""Slice 3: setup_state tests."""

from __future__ import annotations

import json

import pytest

from modulatio import config, setup_state


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE", cfg_dir / "setup-state.json")
    yield


def test_setup_completed_false_when_file_missing():
    assert setup_state.setup_completed() is False


def test_load_returns_empty_when_missing():
    assert setup_state.load() == {}


def test_mark_completed_records_version_and_skipped():
    state = setup_state.mark_completed(version="2.0.0", skipped_steps=["pandoc"])
    assert state["wizard_version"] == "2.0.0"
    assert state["skipped_steps"] == ["pandoc"]
    assert "wizard_completed_at" in state
    assert setup_state.setup_completed() is True


def test_mark_completed_persists_to_disk():
    setup_state.mark_completed(version="2.0.0")
    on_disk = json.loads(setup_state.SETUP_STATE_FILE.read_text())
    assert on_disk["wizard_version"] == "2.0.0"


def test_reset_removes_state():
    setup_state.mark_completed(version="2.0.0")
    assert setup_state.reset() is True
    assert setup_state.setup_completed() is False
    assert setup_state.reset() is False  # second reset is no-op


def test_load_ignores_malformed_file():
    setup_state.SETUP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    setup_state.SETUP_STATE_FILE.write_text("not json {{{")
    assert setup_state.load() == {}
