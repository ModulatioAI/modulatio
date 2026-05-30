# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Modulatio TUI package (slice #20).

Textual-based terminal UI for the business harness. Entry point:
``modulatio-tui`` (declared in ``pyproject.toml``). See ``tui-spec.md``
and ``tui-plan.md`` in the design vault for the full milestone.
"""
from __future__ import annotations

from modulatio.tui.app import run

__all__ = ["run"]
