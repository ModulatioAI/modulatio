# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Budget tracking + cap enforcement for plan execution.

When a plan declares ``max_tokens`` or ``max_cost_usd`` in frontmatter,
``start_execution`` instantiates a ``BudgetTracker`` and binds it to a
ContextVar. Runners (currently litellm-backed) call ``record_usage``
after each completion; the tracker accumulates totals and ``over_cap``
returns a halt reason when a cap is exceeded.

**Cap enforcement semantics — POST-CALL (third-party review note,
2026-05-02):** caps are checked at the TOP of each sub-objective
iteration in ``project_execution.start_execution``, AFTER the previous
sub-objective's kickoff has already run. A single large sub-objective
that consumes more than the remaining budget will overrun before
Modulatio pauses. These caps are floors against runaway accumulation
across many sub-objectives, NOT precise per-call ceilings. Reasonable
ratio: set caps ~1.5x the largest expected sub-objective so a single
oversize sub-objective stays inside the cap; for tighter pre-call
enforcement see  (context-window budget primitive — extends
the same mechanism with pre-flight token estimation).

Usage recording is post-call too: ``record_usage`` fires after a
successful LiteLLM completion. Failed/retried calls that the provider
nonetheless billed for (e.g. timeouts mid-stream) may not show up in
the tracker until hooks per-provider error-response shapes for
billed-on-failure usage extraction.

Cost accounting depends on the runner to compute USD per call. LiteLLM
provides ``completion_cost`` for known providers; unknown / local models
report 0 USD. Token accounting works for any provider that surfaces
``response.usage``; runners that don't (stubs in tests) record nothing,
the tracker stays at zero, and caps simply never trip.

Together with the wall-clock cap shipped earlier, this is the
bounded-mode primitive's instrumentation layer. See
``feedback_modulatio_design_for_both_budget_modes.md`` (memory) for the
two-mode framing — bounded honesty + unbounded capability are both
first-class; structural ceilings (not money) are the unbounded blocker.
"""

from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class BudgetTracker:
    """Per-plan accumulator + cap enforcer + (optional) telemetry log.

    Caps are advisory — ``over_cap`` returns a reason string when any
    cap is breached, but it's the caller's job (typically the
    execution loop) to actually halt. ``None`` cap = unbounded on
    that axis. The accumulator runs regardless so the human sees
    actual spend even when no cap was set.

    When ``log_path`` is set, every ``record`` call appends a JSONL
    line capturing that single completion's usage plus the running
    totals — self-describing per line so the file can be tailed or
    grepped without rebuilding state. Append-only; no rotation. Plans
    typically log 5–500 lines (≪ 100KB).
    """
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    tokens_used: int = 0
    cost_usd_used: float = 0.0
    log_path: Path | None = None

    def record(
        self, *, input_tokens: int, output_tokens: int, cost_usd: float,
        model: str | None = None,
    ) -> None:
        """Add a single completion's usage to the running totals.

        ``model`` is optional metadata for the telemetry log — the
        runner that knows the litellm model id passes it through;
        callers that don't have it (tests, future stub paths) leave
        it None and the log line records ``"model": null``.
        """
        self.tokens_used += int(input_tokens) + int(output_tokens)
        self.cost_usd_used += float(cost_usd)
        if self.log_path is not None:
            self._append_log_line(
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                cost_usd=float(cost_usd),
                model=model,
            )

    def _append_log_line(
        self, *, input_tokens: int, output_tokens: int, cost_usd: float,
        model: str | None,
    ) -> None:
        """Write one JSONL record reflecting THIS call + the running
        totals after it. Failures are swallowed — telemetry must
        never break a real LLM call."""
        try:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(cost_usd, 6),
                "tokens_total": self.tokens_used,
                "cost_total": round(self.cost_usd_used, 6),
            }
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            # Filesystem unavailable / permission denied / disk full —
            # telemetry must never break a real LLM call.
            pass

    def over_cap(self) -> str | None:
        """Halt reason when any cap is exceeded; ``None`` when within
        bounds (or no cap set)."""
        if self.max_tokens is not None and self.tokens_used > self.max_tokens:
            return (
                f"token cap exceeded "
                f"({self.tokens_used} > {self.max_tokens})"
            )
        if self.max_cost_usd is not None and self.cost_usd_used > self.max_cost_usd:
            return (
                f"cost cap exceeded "
                f"(${self.cost_usd_used:.4f} > ${self.max_cost_usd:.4f})"
            )
        return None


_current_tracker: contextvars.ContextVar["BudgetTracker | None"] = (
    contextvars.ContextVar("modulatio_budget_tracker", default=None)
)


def current_tracker() -> "BudgetTracker | None":
    """Return the tracker bound to the current execution context, or
    None when no plan execution is active. Runners read this to find
    where to record their usage."""
    return _current_tracker.get()


def record_usage(
    *, input_tokens: int, output_tokens: int, cost_usd: float,
    model: str | None = None,
) -> None:
    """Module-level convenience for runners. No-op when no tracker is
    bound — runners stay clean of "is there a tracker?" branches.
    ``model`` is optional metadata for the telemetry log.
    """
    tracker = _current_tracker.get()
    if tracker is None:
        return
    tracker.record(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        model=model,
    )


@contextmanager
def with_tracker(tracker: BudgetTracker):
    """Bind ``tracker`` to the ContextVar for the duration of the
    with-block. ``start_execution`` wraps the run loop in this so
    every runner invocation it triggers reaches the same accumulator.
    Restores the previous binding on exit."""
    token = _current_tracker.set(tracker)
    try:
        yield tracker
    finally:
        _current_tracker.reset(token)


def bind(tracker: BudgetTracker):
    """Non-context-manager binding for callers that can't easily wrap
    a long loop body in ``with`` without re-indenting. Returns an
    opaque token to pass back to ``unbind``. Pair with try/finally to
    guarantee cleanup on every exit path.
    """
    return _current_tracker.set(tracker)


def unbind(token) -> None:
    """Restore the prior tracker binding. ``token`` must be the value
    ``bind`` returned."""
    _current_tracker.reset(token)


__all__ = [
    "BudgetTracker",
    "bind",
    "current_tracker",
    "record_usage",
    "unbind",
    "with_tracker",
]
