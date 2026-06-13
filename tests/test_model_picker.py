"""Tests for the ModelPicker widget (Configuration tab, add-model step 3).

Regression coverage for the worker-callback race: the live-model fetch runs on
a worker thread and posts its result back via ``call_from_thread(_on_loaded)``.
If the operator navigates away (the picker is unmounted) before the fetch
finishes, ``_on_loaded`` used to call ``query_one('#mp-status')`` on a detached
widget and crash the main thread with ``NoMatches``.
"""
from __future__ import annotations

import pytest

from modulatio import provider_catalog as pc
from modulatio.tui.widgets.model_picker import ModelPicker


def _cloud_provider() -> pc.Provider:
    """An api-source (cloud) provider — the non-custom path that fetches live."""
    return pc.get_provider("openrouter")


def test_on_loaded_no_op_when_unmounted():
    """The worker callback must bail quietly if the picker was unmounted during
    the fetch — it must not raise NoMatches by touching detached widgets."""
    picker = ModelPicker(_cloud_provider())
    # Never mounted: query_one would raise NoMatches for #mp-status / #mp-list.
    assert picker.is_mounted is False

    models = [
        pc.CatalogModel(
            id="vendor/model-a", name="Model A", provider_id="openrouter"
        )
    ]
    # Must not raise, and must not have populated state from the dead widget.
    picker._on_loaded(models)
    assert picker._models == []


def test_on_loaded_populates_when_mounted():
    """Sanity guard: when the picker IS live, _on_loaded still does its job
    (the early-return only fires on the unmounted path)."""

    populated: dict[str, list] = {}

    class _MountedPicker(ModelPicker):
        # Simulate a live widget without spinning up a full Textual App.
        @property
        def is_mounted(self) -> bool:  # type: ignore[override]
            return True

        def _populate(self, query: str = "") -> None:
            populated["models"] = list(self._models)

        def query_one(self, selector, *args, **kwargs):  # type: ignore[override]
            class _Status:
                def update(self, _text):
                    populated["status"] = _text

            return _Status()

    picker = _MountedPicker(_cloud_provider())
    models = [
        pc.CatalogModel(
            id="vendor/model-a", name="Model A", provider_id="openrouter"
        ),
        pc.CatalogModel(
            id="vendor/img",
            name="Img",
            provider_id="openrouter",
            modality="image",
        ),
    ]
    picker._on_loaded(models)
    # Only the text model drives the picker; image counted as "other".
    assert [m.id for m in picker._models] == ["vendor/model-a"]
    assert populated["models"] == picker._models
    assert "model" in populated["status"].lower()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
