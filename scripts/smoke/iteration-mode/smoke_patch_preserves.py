#!/usr/bin/env python3
"""Increment 3 core: a surgical SEARCH/REPLACE patch changes ONE thing and the
engine keeps every other byte — so a cheap producer can't regen-and-drop working
code (the live regression that lost the A/D, W, X + mouse bindings).

Run:  ~/modulatio/.venv/bin/python smoke_patch_preserves.py
"""
from modulatio.orchestration import (
    _parse_search_replace_blocks, _apply_search_replace,
)

ORIGINAL = (
    "import pygame\n"
    "JUMP = -12\n"
    "MOVE_SPEED = 5\n"
    "def handle_input(keys, mouse):\n"
    "    if keys[K_LEFT] or keys[K_a]: move_left()       # left: arrow or A\n"
    "    if keys[K_RIGHT] or keys[K_d]: move_right()      # right: arrow or D\n"
    "    if keys[K_SPACE] or keys[K_w]: jump()          # space + W\n"
    "    if keys[K_z] or keys[K_x] or mouse[0]: attack() # Z + X + click\n"
)
RESPONSE = (
    "<<<<<<< SEARCH\n"
    "JUMP = -12\n"
    "=======\n"
    "JUMP = -22\n"
    ">>>>>>> REPLACE\n"
    "## summary_for_state_doc\nraised jump\n"
)

blocks = _parse_search_replace_blocks(RESPONSE)
new, applied, failures = _apply_search_replace(ORIGINAL, blocks)
assert applied == 1 and not failures, (applied, failures)
assert "JUMP = -22" in new
for token in ("K_a", "K_d", "K_w", "K_x", "mouse[0]", "MOVE_SPEED = 5"):
    assert token in new, f"DROPPED control/code: {token}"
assert new == ORIGINAL.replace("JUMP = -12", "JUMP = -22"), "non-target bytes changed"
print("PASS: 1-line patch applied; every control + line preserved byte-for-byte")
