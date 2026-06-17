# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Feng-Tui themes — the harmonious terminal interface.

Three Textual themes (amber / green / cyan) on a pure-black phosphor base. Each maps
the Feng-Tui palette onto Modulatio's EXISTING CSS variable names ($primary, $accent,
$secondary, $frame, $frame-dim, $text-muted, ...), so when ``App.theme`` changes every
mounted screen recolours by cascade — no per-screen edits. ``App.theme`` re-resolves
these variables live (the watcher reparses + re-applies the stylesheet to the whole
screen stack).

Pure black is the breathing room; the accent carries meaning through brightness tiers
(``primary``/``accent`` = focal, ``secondary``/``frame-dim`` = the dim glow). ``error``
stays terminal-red in every variant (failures only). ``panel``/``boost`` are left unset
on purpose — overriding ``$panel`` breaks Textual's maximized-view CSS.
"""
from __future__ import annotations

from textual.theme import Theme

#: Pure-black background, aged-parchment-free phosphor base, terminal-red errors.
_BG = "#000000"
_BASE = "#E0E0E0"
_ERROR = "#FF5555"


def _feng(name: str, accent: str, dim: str) -> Theme:
    """One Feng-Tui variant: a single accent hue in two brightness tiers on black."""
    return Theme(
        name=name,
        dark=True,
        background=_BG,
        surface=_BG,
        foreground=_BASE,
        primary=accent,     # focal text / headings / active
        secondary=dim,      # secondary text / meta
        accent=accent,      # the one hot accent
        success=accent,     # monochrome — successes read in the accent family
        warning=dim,
        error=_ERROR,
        # Carried in ``variables`` so they survive App.get_css_variables() and track
        # the active accent: frame chrome + the dim muted tone + the block cursor.
        # The block cursor (active Tab, OptionList highlight) is a SUBTLE dark
        # highlight with bright accent text — never a bright fill that hides the
        # label (the default $block-cursor-background == primary did exactly that).
        variables={
            "frame": accent,
            "frame-dim": dim,
            "text-muted": dim,
            "block-cursor-background": "#1f1f1f",
            "block-cursor-foreground": accent,
            "block-cursor-text-style": "bold",
        },
    )


def theme_tiers(app) -> tuple[str, str, str, str]:
    """(accent, dim, base, error) hex for the ACTIVE theme.

    For Rich ``Text`` styles that can't reference ``$variables`` (the live stream
    feeds, status line) — call this at render time so each rendered line tracks
    the current Feng-Tui variant. accent = focal (primary), dim = secondary,
    base = foreground, error = terminal-red.
    """
    try:
        th = app.current_theme
        return (
            th.primary or "#FFC933",
            th.secondary or "#FFB300",
            th.foreground or "#E0E0E0",
            th.error or "#FF5555",
        )
    except Exception:
        return ("#FFC933", "#FFB300", "#E0E0E0", "#FF5555")


FENG_AMBER = _feng("feng-amber", "#FFC933", "#FFB300")
FENG_GREEN = _feng("feng-green", "#7DFF9C", "#44FF77")
FENG_CYAN = _feng("feng-cyan", "#80EEFF", "#44E8FF")

#: Cycle order (amber is the default).
FENG_THEMES = [FENG_AMBER, FENG_GREEN, FENG_CYAN]
FENG_THEME_NAMES = [t.name for t in FENG_THEMES]

__all__ = [
    "FENG_THEMES", "FENG_THEME_NAMES", "FENG_AMBER", "FENG_GREEN", "FENG_CYAN",
    "theme_tiers",
]
