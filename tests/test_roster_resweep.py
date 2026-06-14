# SPDX-License-Identifier: Apache-2.0
"""0.9.0 pre-ship re-sweep regressions for ``modulatio.roster``.

Dedicated file (own-the-file contract): does not touch ``test_roster.py``
or ``test_roster_r2_audit.py``.
"""

from __future__ import annotations

from modulatio.roster import Agent


def test_capacity_cap_floored_on_direct_construction() -> None:
    """Baseline: the field_validator floors ctor/parse paths (pre-existing)."""
    assert Agent(id="a", name="a", capacity_cap=0).capacity_cap == 1
    assert Agent(id="a", name="a", capacity_cap=-5).capacity_cap == 1
    assert Agent(id="a", name="a", capacity_cap=3).capacity_cap == 3


def test_capacity_cap_floored_on_model_copy_update_zero() -> None:
    """re-sweep finding 1: a sub-1 cap must not enter the roster via the
    ``model_copy(update=...)`` path. Pydantic v2 field_validators do NOT
    fire on model_copy, so before the override this returned 0 (a silent
    non-dispatchable producer)."""
    base = Agent(id="a", name="a", capacity_cap=4)
    copied = base.model_copy(update={"capacity_cap": 0})
    assert copied.capacity_cap == 1


def test_capacity_cap_floored_on_model_copy_update_negative() -> None:
    """re-sweep finding 1: negative caps via copy are floored too."""
    base = Agent(id="a", name="a", capacity_cap=4)
    assert base.model_copy(update={"capacity_cap": -7}).capacity_cap == 1


def test_model_copy_preserves_valid_capacity_cap() -> None:
    """The override must not clobber legitimate caps — only floor sub-1."""
    base = Agent(id="a", name="a", capacity_cap=8)
    assert base.model_copy().capacity_cap == 8
    assert base.model_copy(update={"capacity_cap": 5}).capacity_cap == 5


def test_model_copy_update_unrelated_field_keeps_cap() -> None:
    """The common real call sites (``add_model``/``clear_model``) copy with an
    update that does not touch capacity_cap; the cap must round-trip."""
    base = Agent(id="a", name="a", capacity_cap=2, model="m1")
    assert base.model_copy(update={"model": "m2"}).capacity_cap == 2
