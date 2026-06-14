# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""r2 audit regression: docstrings must match the soft-warn DEBUG demotion.

Finding (LOW/correctness): the module docstring and the
``soft_warn_at_pct`` field doc still claimed the soft-warn band emits a
WARNING, but the code intentionally emits at DEBUG (#167 — advisory
clutter that flooded the user stream was demoted). Stale docs mislead an
operator into hunting for a WARNING log line that never fires. These
tests guard the doc/code agreement so the stale claim can't silently
come back.
"""

from __future__ import annotations

import inspect
import logging

from modulatio import context_budget


def test_module_docstring_describes_soft_warn_as_debug_not_warning() -> None:
    """The module docstring's soft-warn band (state 2) must not promise a
    WARNING; it must describe the DEBUG emission the code actually does."""
    doc = context_budget.__doc__ or ""
    assert "DEBUG" in doc, "module docstring should mention the DEBUG soft-warn level"
    # The old stale phrasing promised a WARNING for the soft-warn band.
    assert "structured WARNING" not in doc, (
        "stale doc: soft-warn band must not claim it emits a WARNING"
    )


def test_field_docstring_describes_soft_warn_as_debug() -> None:
    """The ``soft_warn_at_pct`` field doc inside ContextBudgetConfig's
    docstring must describe a DEBUG-level soft-warn, not a WARNING log."""
    cfg_doc = context_budget.ContextBudgetConfig.__doc__ or ""
    assert "soft_warn_at_pct" in cfg_doc
    # Pull just the soft_warn_at_pct paragraph to avoid matching unrelated
    # mentions of WARNING elsewhere in the dataclass docstring.
    idx = cfg_doc.index("soft_warn_at_pct")
    nxt = cfg_doc.find("prune_at_pct", idx)
    para = cfg_doc[idx : nxt if nxt != -1 else len(cfg_doc)]
    assert "DEBUG" in para, "soft_warn_at_pct doc should describe a DEBUG-level log"
    assert "WARNING" not in para, (
        "stale doc: soft_warn_at_pct must not claim a WARNING log is emitted"
    )


def test_soft_warn_actually_emits_at_debug() -> None:
    """Behavioral anchor: the soft-warn band emits exactly one structured
    record at DEBUG (the doc-fix must stay consistent with real behavior)."""
    # Mirror the boundaries from the existing soft-warn band test so the
    # padded estimate lands inside [soft_warn, prune) and does not refuse.
    msgs = [{"role": "user", "content": "y" * 1000}]
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=400,
        soft_warn_at_pct=0.50,
        prune_at_pct=0.95,
        pad_pct=0.0,
    )
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("modulatio.context_budget")
    handler = _Capture()
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        out, did_warn = context_budget.check_and_compress(
            msgs, model="gpt-4o-mini", call_id="r2-warn", config=cfg
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)

    assert out is msgs
    assert did_warn is True
    soft = [
        r for r in records
        if getattr(r, "modulatio_event", None) == "context_budget_soft_warn"
    ]
    assert len(soft) == 1
    assert soft[0].levelname == "DEBUG"


def test_no_source_module_imported_cleanly() -> None:
    """Sanity: the module imports and the gate function is present."""
    assert inspect.isfunction(context_budget.check_and_compress)
