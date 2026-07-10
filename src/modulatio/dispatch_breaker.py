# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Per-dispatch circuit breaker — bound a runaway producer (QC-as-fixer Slice 2).

The context-budget primitive (``context_budget.py``) bounds **input**
tokens before a call. It does NOT bound how much a producer *generates*
in one dispatch. In observed cases, a sparse local MoE
producer ran away inside a SINGLE dispatch — ~36K output tokens, zero
``write_artifact`` calls, never committed an artifact. Nothing stopped it.

This module is the missing output-side bound. It is deliberately:

- **POST-HOC (Phase A).** The runner seam (``_run_agent_call``) returns a
  *completed* string, not a chunk stream — so this analyzes the finished
  response. It bounds *retries* and feeds the self-heal handoff; it does
  NOT abort generation mid-stream. True mid-stream abort needs a streaming
  runner contract (Phase B), deferred.
- **Trip on NO-PROGRESS, not volume.** A legit 1,500-line file is ~15–20K
  tokens of *monotonic* growth and must NOT be killed. A storm *churns*:
  degenerate repetition, or huge output with nothing committed. The soft
  output ceiling ALONE never trips — only repetition, no-commit, or the
  absolute hard backstop do.
- **Worker-local + pure.** No shared mutable state; safe to run inside an
  isolated concurrent worker without violating the merge-authoritative
  contract. The orchestrator decides what to do with a returned abort.

Flag-gated by ``MODULATIO_DISPATCH_BREAKER=1`` — OFF by default, so the
production path is byte-for-byte unchanged until the breaker is tuned.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from typing import Literal

#: Per-role per-dispatch OUTPUT-token budget. DISTINCT from the input-only
#: ``ContextBudgetConfig`` (do not overload ``max_input_tokens``).
#: ``soft_cap`` is an observe/pair-only threshold (never trips alone); the
#: hard backstop derives as ``soft_cap * 2`` (so the default 30K → 60K hard).
_DEFAULT_SOFT_CAP = 30_000
#: Built-in per-role overrides of the soft cap (~30K producer default).
#: Project/agent overrides (Project.output_budgets) layer ON TOP of
#: this at resolve time and win.
_ROLE_SOFT_CAPS: dict[str, int] = {}
#: Validation bounds for a Project.output_budgets soft-cap override
#: (#151). A floor keeps a typo from bounding every dispatch to nothing;
#: the ceiling keeps "unbounded" from sneaking in disguised as a number.
OUTPUT_BUDGET_MIN = 1_000
OUTPUT_BUDGET_CEILING = 200_000

#: A committed artifact shorter than this (stripped chars) counts as
#: "nothing committed" for the no-commit storm signal.
#: No-commit only trips once the producer has generated at least this much
#: raw output — otherwise a legitimately short empty result would trip.
_NO_COMMIT_MIN_OUTPUT_TOKENS = 500

#: Degenerate-repetition signature: an n-gram (this many whitespace tokens)
#: repeating at least ``_REPEAT_THRESHOLD`` times is a stuck loop. This count
#: threshold is PROSE-tuned — prose almost never repeats a 12-token phrase 5×
#: legitimately.
_NGRAM_N = 12
_REPEAT_THRESHOLD = 5

#: Families whose deliverable is PROSE — the strict count threshold above is the
#: right degenerate-loop signature for them.
_PROSE_FAMILIES = frozenset({"document"})

#: For STRUCTURED families (code / data / media), repetition is LEGITIMATE — a
#: markdown table, a repeated config block, a media layout, identical data rows.
#: Applying the prose-tuned count threshold there false-trips and DISCARDS a
#: committed-good deliverable (a product-agnostic violation: the universal unit
#: is the token, and a bounded structural repeat is a valid artifact, not a
#: storm). So for those families we trip ONLY on a DEGENERATE loop — one where
#: the single most-repeated n-gram DOMINATES the output, filling at least this
#: fraction of it. ``hard_cap`` (runaway tokens) and ``no_commit`` (no progress)
#: remain the family-agnostic backstops, and QC reviews the deliverable besides.
_STRUCTURED_REPEAT_COVERAGE = 0.5

AbortReason = Literal["repetition", "no_commit", "hard_cap"]


@dataclass(frozen=True)
class OutputBudget:
    """Resolved per-dispatch output bounds for one role."""

    role: str
    soft_cap: int
    hard_cap: int


class DispatchAbort(Exception):
    """A producer dispatch was bound by the circuit breaker.

    STRUCTURED, not a bare string: carries the reason,
    the role, the output-token count, a human detail, and the committed
    partial text (for Slice-3 salvage). The orchestrator catches this
    SEPARATELY from generic exceptions so a bound dispatch enters the
    self-heal path rather than the runtime-failure ``BLOCKED`` path.
    """

    def __init__(
        self,
        reason: AbortReason,
        *,
        role: str,
        output_tokens: int,
        detail: str,
        partial_text: str = "",
    ) -> None:
        self.reason = reason
        self.role = role
        self.output_tokens = output_tokens
        self.detail = detail
        self.partial_text = partial_text
        super().__init__(f"dispatch aborted ({reason}, {output_tokens} tok): {detail}")

    @property
    def summary(self) -> str:
        return f"{self.reason} ({self.output_tokens} output tokens): {self.detail}"


def breaker_enabled() -> bool:
    """True when ``MODULATIO_DISPATCH_BREAKER=1``. OFF by default."""
    return os.environ.get("MODULATIO_DISPATCH_BREAKER", "").strip() == "1"


def resolve_output_budget(
    role: str, *, overrides: "dict[str, int] | None" = None
) -> OutputBudget:
    """Per-role output budget. Resolution order for the soft cap (#151):
    project/agent ``overrides`` (e.g. ``Project.output_budgets``) win, then
    the built-in ``_ROLE_SOFT_CAPS``, then ``_DEFAULT_SOFT_CAP``. The hard
    backstop derives from the soft cap as ``soft * 2``, so an override moves
    the bound in BOTH directions (raising it for a denser model, or
    tightening it)."""
    soft = None
    if overrides:
        soft = overrides.get(role)
    if soft is None:
        soft = _ROLE_SOFT_CAPS.get(role, _DEFAULT_SOFT_CAP)
    # Hard backstop derives from the soft cap (×2) so an override controls
    # the bound in BOTH directions — raising it for a denser model that
    # legitimately needs more, or tightening it. The default 30K → 60K hard
    # (== _DEFAULT_HARD_CAP) is unchanged.
    return OutputBudget(role=role, soft_cap=soft, hard_cap=soft * 2)


def _output_token_count(text: str) -> int:
    """Approximate TOKEN count — NOT a bare whitespace word count.

    The runaway cost/OOM backstops compare this against token caps, and the
    universal unit is the token. A compact/minified deliverable (one-line JSON,
    minified code, base64, CJK prose) has FEW whitespace 'words' but MANY real
    tokens, so a word count would let the backstop fire far too late — or never —
    for non-prose families (product-agnostic). char/4 is the standard token
    estimate and never undercounts whitespace-light output; the word-count floor
    keeps it sane on the rare token-dense-but-space-light edge.

    Note: the ``len(text.split())`` arm inside
    the ``max()`` is INTENTIONAL, not a residual word-unit gate — ``max()`` picks
    the HIGHER (more conservative) estimate, so this never undercounts a backstop.
    Do not "simplify" to
    char/4-only — that would undercount space-heavy short-word text."""
    return max(len(text) // 4, len(text.split()))


def _max_ngram_repeat(text: str, n: int = _NGRAM_N) -> int:
    """Max number of times any contiguous ``n``-token window repeats.

    Cheap O(tokens) Counter over the sliding window. A monotonic artifact
    has near-1 repeats; a degenerate loop spikes. Returns 0 when the text
    is shorter than one window.
    """
    tokens = text.split()
    if len(tokens) < n:
        return 0
    counts = Counter(
        " ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)
    )
    return max(counts.values())


def analyze_output(
    raw_response: str,
    committed_text: str,
    *,
    role: str,
    budget: OutputBudget | None = None,
    overrides: "dict[str, int] | None" = None,
    artifact_family: str = "document",
) -> DispatchAbort | None:
    """Post-hoc storm detection on a completed producer response.

    Returns a :class:`DispatchAbort` when the dispatch shows NO-PROGRESS
    (degenerate repetition, or substantial output with nothing committed)
    or blows the absolute hard backstop. Returns ``None`` for healthy
    dispatches — including large monotonic artifacts (soft cap alone does
    NOT trip). ``overrides`` (e.g. ``Project.output_budgets``) feed
    per-role budget resolution when ``budget`` isn't passed directly.
    """
    budget = budget or resolve_output_budget(role, overrides=overrides)
    output_tokens = _output_token_count(raw_response)

    # Absolute hard backstop — trips regardless (cost / OOM protection).
    if output_tokens >= budget.hard_cap:
        return DispatchAbort(
            "hard_cap",
            role=role,
            output_tokens=output_tokens,
            detail=(
                f"output {output_tokens} tok >= hard backstop {budget.hard_cap}"
            ),
            partial_text=committed_text,
        )

    # Degenerate repetition — a stuck loop regardless of token count. PROSE
    # families trip on a strict identical-phrase COUNT; STRUCTURED families
    # (code/data/media), where repetition is legitimate, trip only when the
    # repeated phrase DOMINATES the output (a true runaway loop), so a bounded
    # structural repeat — a table, a repeated config block, a media layout — is
    # not discarded (product-agnostic).
    repeat = _max_ngram_repeat(raw_response)
    if artifact_family in _PROSE_FAMILIES:
        repetition_tripped = repeat >= _REPEAT_THRESHOLD
        repeat_detail = (
            f"a {_NGRAM_N}-token phrase repeated {repeat}x "
            f"(>= {_REPEAT_THRESHOLD}) — degenerate loop"
        )
    else:
        total_tokens_ws = len(raw_response.split()) or 1
        coverage = (repeat * _NGRAM_N) / total_tokens_ws
        repetition_tripped = coverage >= _STRUCTURED_REPEAT_COVERAGE
        repeat_detail = (
            f"a {_NGRAM_N}-token phrase repeated {repeat}x, filling "
            f"{coverage:.0%} of the output (>= "
            f"{_STRUCTURED_REPEAT_COVERAGE:.0%}) — degenerate loop "
            f"({artifact_family} family: bounded structural repetition is allowed)"
        )
    if repetition_tripped:
        return DispatchAbort(
            "repetition",
            role=role,
            output_tokens=output_tokens,
            detail=repeat_detail,
            partial_text=committed_text,
        )

    # No-commit storm — generated substantial output, committed NOTHING. This
    # is the no-progress signal that lets the soft ceiling matter; the ceiling
    # never trips on its own. "Nothing" means an EMPTY (whitespace-only) commit,
    # NOT a small one: a compact-but-valid deliverable (a one-line JSON, a terse
    # data answer, a tiny code stub) is a real artifact for the code/data
    # families and must not be discarded as no-progress (product-agnostic — QC
    # judges its quality; the breaker only catches a true empty storm).
    if (
        not committed_text.strip()
        and output_tokens >= _NO_COMMIT_MIN_OUTPUT_TOKENS
    ):
        return DispatchAbort(
            "no_commit",
            role=role,
            output_tokens=output_tokens,
            detail=(
                f"generated {output_tokens} tok but committed nothing — "
                f"produced no artifact"
            ),
            partial_text=committed_text,
        )

    return None


__all__ = [
    "AbortReason",
    "DispatchAbort",
    "OutputBudget",
    "analyze_output",
    "breaker_enabled",
    "resolve_output_budget",
]
