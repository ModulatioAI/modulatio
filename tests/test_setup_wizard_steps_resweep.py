# SPDX-License-Identifier: Apache-2.0
"""Re-sweep regression for the Models-step BACK / staged-api-keys defect.

Finding 1 [MEDIUM/correctness], filed at steps.py:228 (the
``pop_state(steps[step_idx], state)`` call site in ``run_step_machine``).

Root cause is NOT in the product-agnostic state-machine framework
(steps.py) — that module correctly knows nothing about ``staged_api_keys``.
The data-specific defect lives one layer up, in
``modulatio.setup_wizard.__init__._pop_state``: its ``keys_per_step``
table pops BOTH ``staged_api_keys`` and ``configured_models`` for the
"models" step on BACK.

But the models step (provider_step.run) persists model_presets.json to
disk *immediately* (add_preset writes through). The pasted API-key VALUES,
however, live ONLY in ``state['staged_api_keys']``. So pressing BACK on the
Models step drops the in-memory key values while the presets that reference
them survive on disk. On re-entry ``run()`` does
``setdefault('staged_api_keys', {})`` — a fresh empty dict — leaving a
half-configured model: the preset is present, but its key is no longer
staged, so finalize won't write it to .env and the provider summary loses
it. The fix: do NOT pop ``staged_api_keys`` on BACK from the models step
(mirroring how the presets themselves persist on disk).

These tests pin both halves of the contract:

  * the steps.py framework faithfully invokes the caller-supplied
    ``pop_state`` on BACK (the call site named in the finding), and
  * the caller's ``_pop_state`` must PRESERVE ``staged_api_keys`` for the
    models step so the on-disk presets and the in-memory key values stay
    consistent across a BACK/re-enter.

The second test FAILS until __init__._pop_state stops popping
'staged_api_keys' for the 'models' step.
"""

from __future__ import annotations

from modulatio import setup_wizard
from modulatio.setup_wizard import steps


# === Framework-level: run_step_machine BACK -> pop_state contract ===

def test_run_step_machine_calls_pop_state_on_back():
    """BACK from a non-first step pops the PREVIOUS-... — actually the
    current step's state — via the caller's pop_state, then steps back.

    Pins the exact behaviour at steps.py:227-228 the finding references:
    on BACK, ``pop_state(steps[step_idx], state)`` is invoked for the step
    the user is leaving, and the machine decrements the index.
    """
    state: dict = {"a_key": 1, "b_key": 2}
    popped: list[str] = []

    def pop_state(step_name: str, st: dict) -> None:
        popped.append(step_name)

    # Step "a" always advances. Step "b" goes BACK on its FIRST visit only,
    # then advances on its second visit so the machine terminates (else an
    # always-BACK "b" oscillates a<->b forever).
    calls: dict[str, int] = {"a": 0, "b": 0}

    def dispatch(step_name: str, st: dict):
        calls[step_name] += 1
        if step_name == "a":
            return "ok"  # always advance to b
        # step "b": BACK on first visit, advance on the second
        if calls["b"] == 1:
            return steps.BACK
        return "ok"

    steps.run_step_machine(
        state, ["a", "b"], dispatch, pop_state=pop_state
    )

    # BACK was pressed once on "b", so pop_state was called once for "b".
    assert popped == ["b"]
    assert calls["a"] == 2  # entered, advanced to b, returned to and re-advanced
    assert calls["b"] == 2  # first visit BACK, second visit advance


def test_run_step_machine_back_on_first_step_does_not_pop():
    """BACK on the first step is a no-op (no pop, no underflow)."""
    popped: list[str] = []

    def pop_state(step_name: str, st: dict) -> None:
        popped.append(step_name)

    seen = {"n": 0}

    def dispatch(step_name: str, st: dict):
        seen["n"] += 1
        if seen["n"] == 1:
            return steps.BACK  # ignored at first step
        return "ok"  # then complete

    steps.run_step_machine(state={}, steps=["only"], dispatch=dispatch, pop_state=pop_state)
    assert popped == []  # never popped — we were at the first step


# === Caller-level: the actual defect — staged keys must survive BACK ===

# The one-line fix lives in modulatio.setup_wizard.__init__._pop_state: the
# 'models' step's pop list no longer contains 'staged_api_keys'. The symptom
# surfaces at steps.py:228 (the pop_state call site), but the data-specific
# defect is the caller's pop table — fixed in lockstep with this resweep.
def test_pop_state_models_preserves_staged_api_keys():
    """BACK out of the Models step must NOT discard staged API-key values.

    The presets that reference these keys are already on disk and survive
    re-entry; dropping the key values leaves a half-configured model whose
    key is never written to .env. _pop_state must keep 'staged_api_keys'.
    """
    state = {
        "staged_api_keys": {"OPENAI_API_KEY": "sk-live-value"},
        "configured_models": ["gpt-x"],
    }

    setup_wizard._pop_state("models", state)

    # The pasted key value must remain in memory so finalize still writes it
    # and it stays consistent with the on-disk preset that references it.
    assert state.get("staged_api_keys") == {"OPENAI_API_KEY": "sk-live-value"}


