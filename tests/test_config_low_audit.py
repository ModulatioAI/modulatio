"""LOW-audit regression tests for src/modulatio/config.py.

Isolated in a dedicated file to avoid colliding with concurrent edits to
``test_config.py``. Mirrors that module's config-isolation fixture.
"""

from __future__ import annotations

import pytest

from modulatio import config


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Redirect CONFIG_DIR / DEFAULTS_FILE to a tmp dir, scrub XDG env
    vars, and reset the module cache so each test starts clean."""
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    config.reload()
    yield
    config.reload()


def test_budget_caps_rejects_json_bool_for_max_wall_clock_min():
    """Finding #55: a JSON ``true`` for ``max_wall_clock_min`` must NOT
    pass the int/float isinstance gate (bool is an int subclass) and
    silently become ``float(True) == 1.0`` — a 1-minute wall-clock cap.
    Like the other two axes, a bool is malformed → surfaces as None.
    """
    config.save_defaults({
        "budget_caps": {
            "max_wall_clock_min": True,
        }
    })
    caps = config.get_default_budget_caps()
    assert caps["max_wall_clock_min"] is None


def test_budget_caps_rejects_json_false_for_max_wall_clock_min():
    """Symmetry guard: ``false`` (== 0) must also be rejected, not
    become a 0-minute cap."""
    config.save_defaults({
        "budget_caps": {
            "max_wall_clock_min": False,
        }
    })
    caps = config.get_default_budget_caps()
    assert caps["max_wall_clock_min"] is None


def test_budget_caps_still_accepts_real_wall_clock_value():
    """The bool guard must not regress legitimate int/float values."""
    config.save_defaults({
        "budget_caps": {
            "max_wall_clock_min": 30,
        }
    })
    caps = config.get_default_budget_caps()
    assert caps["max_wall_clock_min"] == 30.0
