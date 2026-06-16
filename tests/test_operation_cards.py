# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Per-operation producer cards — the approach guidance injected into a producer's
brief. The principle card is universal; the production card is the operation's; an
un-triaged operation gets construct's (strict) card, never a loose generic.
"""
from __future__ import annotations

import pytest

from modulatio.operation_bars import known_operations
from modulatio.operation_cards import principle_card, production_card, render


def test_principle_card_is_present_and_substantive():
    assert len(principle_card()) > 40


def test_every_operation_has_a_production_card_matching_the_bar_taxonomy():
    # The card keys must match the bar taxonomy EXACTLY — no operation without a card,
    # no card without an operation.
    for op in known_operations():
        assert production_card(op), f"no production card for {op}"
    # And no extras: every card maps to a known operation.
    from modulatio import operation_cards
    assert set(operation_cards._PRODUCTION_CARDS) == set(known_operations())


def test_production_card_is_empty_for_unknown_or_blank():
    for bad in ("", None, "   ", "not-an-op"):
        assert production_card(bad) == ""


def test_production_card_is_case_and_whitespace_insensitive():
    assert production_card("  Debug ") == production_card("debug")


def test_render_includes_principle_and_the_operation_card():
    block = render("debug")
    assert principle_card() in block
    assert production_card("debug") in block
    assert "debug" in block


def test_render_falls_back_to_construct_for_unknown_or_blank():
    # No loose generic: an un-triaged task renders the construct (strict) card,
    # and still always carries the principle card.
    for blank in ("", None, "not-a-real-op"):
        block = render(blank)
        assert principle_card() in block
        assert production_card("construct") in block
        assert "construct" in block


def test_render_always_carries_the_principle_card_for_every_operation():
    for op in known_operations():
        assert principle_card() in render(op)


@pytest.mark.parametrize("op", known_operations())
def test_rendered_card_is_token_lean(op):
    # Budget guard (H-9): principle + one production card stays well under the
    # ~500-token combined ceiling. Approx chars/4 as a token proxy.
    approx_tokens = len(render(op)) / 4
    assert approx_tokens < 300, f"{op} card ~{approx_tokens:.0f} tokens — too heavy"
