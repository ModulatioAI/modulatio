# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""#13: the canonical producer contract bakes in user-agnostic instruction-following
discipline (execute the contract; don't re-plan, over-research, or pad) so a producer
stays on-contract regardless of model or host config — the prose-bend complement to
engine-enforced thinking-off (#16). A prompt nudge bends a model rather than binding it,
so the test locks the discipline's PRESENCE; behavior is validated live, not in a unit."""
from pathlib import Path

import modulatio


def _producer_contract() -> str:
    raw = (Path(modulatio.__file__).parent / "_seed_skills" / "drafter.md").read_text().lower()
    return " ".join(raw.split())  # collapse line-wraps so phrase matches survive wrapping


def test_producer_contract_carries_on_contract_discipline():
    prompt = _producer_contract()
    # Execute the contract; don't re-plan / over-research / expand scope.
    assert "on contract" in prompt
    assert "smallest artifact" in prompt
    assert "more is not better" in prompt
