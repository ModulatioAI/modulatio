"""Round-2 audit regression for semantic_router (LOW/integration).

Finding: semantic_router hardcoded ``_CACHE_ROOT = ~/.cache/modulatio/semantic``,
ignoring ``config.get_cache_root()`` (the wizard ``cache_root`` override and
``$XDG_CACHE_HOME``). The cache root is now resolved per-call via
``_cache_root()`` so config/wizard overrides land in one place — mirroring
``qc_history._cache_root``.
"""

from __future__ import annotations

from pathlib import Path

from modulatio import config, semantic_router


def test_cache_dir_honors_config_cache_root(monkeypatch, tmp_path: Path) -> None:
    """``_cache_dir`` must derive from ``config.get_cache_root()/semantic``,
    not a hardcoded ``~/.cache`` path. Before the fix this returned the
    home-dir cache regardless of the wizard override."""
    override = tmp_path / "wizard-cache"
    monkeypatch.setattr(config, "get_cache_root", lambda: override)

    got = semantic_router._cache_dir("ABC")

    assert got == override / "semantic" / "abc"


def test_cache_root_is_resolved_per_call(monkeypatch, tmp_path: Path) -> None:
    """Per-call resolution: a config change between calls is reflected
    without a process restart (the import-time-constant bug would freeze
    the first value)."""
    first = tmp_path / "one"
    monkeypatch.setattr(config, "get_cache_root", lambda: first)
    assert semantic_router._cache_root() == first / "semantic"

    second = tmp_path / "two"
    monkeypatch.setattr(config, "get_cache_root", lambda: second)
    assert semantic_router._cache_root() == second / "semantic"


def test_cache_root_honors_xdg_cache_home(monkeypatch, tmp_path: Path) -> None:
    """End-to-end through config: setting ``$XDG_CACHE_HOME`` (no wizard
    override) flows into the semantic cache location."""
    # No defaults.json cache_root override -> config falls back to XDG.
    monkeypatch.setattr(config, "_load_defaults", lambda: {})
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    root = semantic_router._cache_root()

    assert root == tmp_path / "xdg" / "modulatio" / "semantic"
