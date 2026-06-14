# SPDX-License-Identifier: Apache-2.0
"""Regression: Configuration·Models list DataTable must not raise MarkupError
on an operator-authored custom model id / preset key containing markup-closer
brackets (preship sweep, configuration.py:118).

``_fill_table`` populates a textual ``DataTable``. Textual's
``default_cell_formatter`` runs every ``str`` cell through ``Text.from_markup``,
which raises ``rich.errors.MarkupError`` on sequences like ``[/v2]`` at paint
time and crashes the TUI. The custom-provider flow lets the operator type an
arbitrary model id / preset key, so the painted cells (key, model, auth_type)
are escaped before they reach the table.

We exercise the real ``_fill_table`` against a fake table that captures the
cells it would paint, then run each captured cell through the exact formatter
the DataTable uses — proving the escape keeps paint from raising while the
literal brackets survive.
"""
from __future__ import annotations

from rich.errors import MarkupError as RichMarkupError
from textual.widgets._data_table import default_cell_formatter

from modulatio import model_presets
from modulatio.tui.screens.configuration import ConfigScreen


class _CapturingTable:
    """Stand-in for DataTable that records the cells ``add_row`` is given —
    no active-app context required (real DataTable.add_row needs one)."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def add_row(self, *cells, key=None):  # noqa: D401 - mimics DataTable
        self.rows.append(cells)


def _render_cell(value: object) -> object:
    """Render a cell the way DataTable paints it — raising MarkupError if the
    cell's markup is invalid."""
    return default_cell_formatter(value)


def test_default_cell_formatter_raises_on_unescaped_bracket_closer():
    """Sanity: the hazard is real — a raw bracket-closer string crashes the
    formatter the DataTable uses to paint."""
    try:
        _render_cell("my-model[/v2]")
    except RichMarkupError:
        return
    raise AssertionError("expected MarkupError on unescaped bracket closer")


def test_fill_table_escapes_bracketed_custom_model(monkeypatch):
    """The fix: a custom preset with bracket-bearing key / model / auth_type
    is escaped, so every painted cell survives the formatter that crashes on
    raw markup-closers — and the literal brackets are preserved."""
    bad_presets = {
        "my-key[/v2]": {
            "model": "vendor/model[/x]",
            "auth_type": "api_key[/oops]",
        }
    }
    monkeypatch.setattr(model_presets, "load_presets", lambda: bad_presets)
    monkeypatch.setattr(model_presets, "is_available", lambda key: False)

    table = _CapturingTable()
    # Bind the unbound method without constructing the full screen/app.
    ConfigScreen._fill_table(object.__new__(ConfigScreen), table)

    assert len(table.rows) == 1
    for cell in table.rows[0]:
        if isinstance(cell, str):
            _render_cell(cell)  # must not raise

    # Literal brackets preserved (escaped, not parsed away as markup).
    assert "my-key[/v2]" in str(_render_cell(table.rows[0][0]))
    assert "vendor/model[/x]" in str(_render_cell(table.rows[0][1]))
