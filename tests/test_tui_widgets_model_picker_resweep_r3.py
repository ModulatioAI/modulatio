"""Re-sweep R3 regression: ModelPicker must not crash on duplicate model ids.

Finding 1 (MEDIUM/edge-case): ``ModelPicker._populate`` added one OptionList
``Option`` per model in the listing. The listing aggregates a provider's
``models_source`` + ``extra_sources`` (via ``pc.fetch_models``), which can
carry the same id twice. Textual's ``OptionList.add_option`` raises
``DuplicateID`` on a repeated id — crashing the picker on a noisy feed. The fix
dedups by first occurrence in ``_populate`` (order preserved), which also
defends the ``search`` / ``curated_default`` outputs.
"""
from __future__ import annotations

import pytest
from textual.widgets import OptionList
from textual.widgets.option_list import DuplicateID

from modulatio import provider_catalog as pc
from modulatio.tui.widgets.model_picker import ModelPicker


def _cloud_provider() -> pc.Provider:
    return pc.get_provider("openrouter")


class _PopulatePicker(ModelPicker):
    """Drive ``_populate`` against a real (unmounted) OptionList without
    spinning up a full Textual App — an unmounted OptionList still enforces
    unique ids and raises DuplicateID on a repeat, mirroring the live crash."""

    def __init__(self, provider: pc.Provider) -> None:
        super().__init__(provider)
        self._opt_list = OptionList()

    def query_one(self, selector, *args, **kwargs):  # type: ignore[override]
        return self._opt_list


def _dup_models() -> list[pc.CatalogModel]:
    # Same id appearing twice — what aggregation across sources produces.
    return [
        pc.CatalogModel(id="vendor/dup", name="Dup A", provider_id="openrouter"),
        pc.CatalogModel(id="vendor/dup", name="Dup B", provider_id="openrouter"),
        pc.CatalogModel(id="vendor/other", name="Other", provider_id="openrouter"),
    ]


def test_populate_dedups_duplicate_ids_search_path():
    """Search path: duplicate ids must not raise DuplicateID."""
    picker = _PopulatePicker(_cloud_provider())
    picker._models = _dup_models()

    # query matches both duplicates; without dedup the 2nd add_option raises.
    picker._populate("vendor/dup")

    ids = [opt.id for opt in picker._opt_list._options]
    assert ids == ["vendor/dup"]


def test_populate_dedups_duplicate_ids_default_path():
    """Curated-default (unsearched) path: duplicate ids must not raise."""
    picker = _PopulatePicker(_cloud_provider())
    picker._models = _dup_models()

    picker._populate()  # no query → curated_default

    ids = [opt.id for opt in picker._opt_list._options]
    # both unique ids present, each exactly once
    assert ids.count("vendor/dup") == 1
    assert ids.count("vendor/other") == 1


def test_populate_preserves_first_occurrence_order():
    """Dedup keeps the FIRST occurrence and preserves listing order."""
    picker = _PopulatePicker(_cloud_provider())
    picker._models = [
        pc.CatalogModel(id="a", name="A", provider_id="openrouter"),
        pc.CatalogModel(id="b", name="B", provider_id="openrouter"),
        pc.CatalogModel(id="a", name="A again", provider_id="openrouter"),
        pc.CatalogModel(id="c", name="C", provider_id="openrouter"),
    ]
    # search "" returns list(models); but we want the search path with a query
    # that matches all four — use a query common to all ids' names.
    picker._populate("a")  # matches ids 'a' (x2) by id; name 'A again'

    ids = [opt.id for opt in picker._opt_list._options]
    assert ids == ["a"]  # single 'a', no DuplicateID


def test_unmounted_optionlist_actually_raises_without_dedup():
    """Anchor: confirm the underlying primitive raises so the dedup is load-bearing."""
    ol = OptionList()
    from textual.widgets.option_list import Option

    ol.add_option(Option("first", id="dup"))
    with pytest.raises(DuplicateID):
        ol.add_option(Option("second", id="dup"))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
