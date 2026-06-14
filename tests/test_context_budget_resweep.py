"""0.9.0 pre-ship re-sweep regression — Finding #611.

Compression-success (and refusal) telemetry must carry the
post-compression token count so the documented compression-ratio join
is actually computable off the emitted ``context_budget`` audit row.

Before the fix ``_emit_context_budget_event`` had no
``post_compression_tokens`` field and ``check_and_compress`` only passed
the pre-prune estimate; the inline comment promised a ratio join against
``padded_after`` that no consumer could perform.

Production/producer-agnostic: assertions are purely on TOKEN counts and
the derived ratio — no artifact-class assumptions.
"""

from __future__ import annotations

import json
from pathlib import Path

from modulatio import context_budget as cb


def _read_ctx_rows(audit_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in audit_path.read_text().splitlines()
        if line.strip()
        and json.loads(line).get("actor") == "context_budget"
    ]


def _compress_band_messages() -> list[dict]:
    # 8 large tool messages -> trip the soft-compress band, prune the
    # oldest to placeholders, return a shorter list that fits. Mirrors
    # the existing in-band test fixture.
    big = "y" * 1000
    return [{"role": "user", "content": "p"}] + [
        {"role": "tool", "tool_call_id": f"c{i}", "content": big}
        for i in range(8)
    ]


def test_emit_event_accepts_post_compression_tokens_and_derives_ratio(
    tmp_path: Path,
) -> None:
    """_emit_context_budget_event must persist post_compression_tokens
    and a derived compression_ratio when given the post-prune count."""
    audit = tmp_path / "audit.jsonl"
    with cb.dispatch_context(
        budget_role="producer",
        runner_role="drafter",
        model="gpt-4o-mini",
        project_code="RSW",
        run_id="r1",
        audit_path=audit,
    ):
        cb._emit_context_budget_event(
            model="gpt-4o-mini",
            call_id="t",
            pre_compression_tokens=1000,
            post_compression_tokens=400,
            soft_warn_fired=False,
            compression_fired=True,
            refusal_fired=False,
        )
    rows = _read_ctx_rows(audit)
    assert rows, "no context_budget row emitted"
    row = rows[-1]
    # The field must exist (was absent before the fix).
    assert "post_compression_tokens" in row
    assert row["post_compression_tokens"] == 400
    assert row["pre_compression_tokens"] == 1000
    assert row["compression_ratio"] == 0.4


def test_no_compression_branch_leaves_post_tokens_none(tmp_path: Path) -> None:
    """A non-compression emission (e.g. under-threshold) must report
    post_compression_tokens=None and compression_ratio=None — back-compat
    default for callers that don't pass the new field."""
    audit = tmp_path / "audit.jsonl"
    with cb.dispatch_context(
        budget_role="producer",
        runner_role="drafter",
        model="gpt-4o-mini",
        project_code="RSW",
        run_id="r2",
        audit_path=audit,
    ):
        cb._emit_context_budget_event(
            model="gpt-4o-mini",
            call_id="t",
            pre_compression_tokens=500,
            soft_warn_fired=False,
            compression_fired=False,
            refusal_fired=False,
        )
    row = _read_ctx_rows(audit)[-1]
    assert row["post_compression_tokens"] is None
    assert row["compression_ratio"] is None


def test_compression_success_row_carries_both_token_ends(tmp_path: Path) -> None:
    """End-to-end through check_and_compress: the soft-compress-success
    path must emit a row whose post_compression_tokens < the cap and
    whose compression_ratio is computable (< 1.0)."""
    audit = tmp_path / "audit.jsonl"
    outer = cb.ContextBudgetConfig(
        max_input_tokens=1500,  # tight ceiling, prune_at 80% = 1200
        prune_at_pct=0.80,
        pad_pct=0.0,
        keep_recent=2,
    )
    with cb.with_config(outer):
        with cb.dispatch_context(
            budget_role="producer",
            runner_role="drafter",
            model="gpt-4o-mini",
            project_code="RSW",
            run_id="r3",
            audit_path=audit,
        ):
            out, _ = cb.check_and_compress(
                _compress_band_messages(),
                model="gpt-4o-mini",
                call_id="t",
            )
    # Sanity: a prune actually happened (compress band, not refusal).
    assert any(
        str(m.get("content", "")).startswith("[summarized:")
        for m in out
        if m.get("role") == "tool"
    )
    rows = _read_ctx_rows(audit)
    fired = [r for r in rows if r.get("compression_fired") and not r.get("refusal_fired")]
    assert fired, "no compression-success row emitted"
    row = fired[-1]
    assert row["post_compression_tokens"] is not None
    assert row["post_compression_tokens"] < 1500
    assert row["compression_ratio"] is not None
    assert 0.0 < row["compression_ratio"] < 1.0
    # The documented join: ratio reconstructs post/pre off the row alone.
    assert (
        abs(
            row["compression_ratio"]
            - row["post_compression_tokens"] / row["pre_compression_tokens"]
        )
        < 1e-9
    )


def test_refusal_row_carries_post_compression_tokens(tmp_path: Path) -> None:
    """The refuse-with-checkpoint path also pruned; its row must carry the
    post-prune count that still overflowed (so ratio analysis can see a
    prune that didn't help)."""
    audit = tmp_path / "audit.jsonl"
    big = "z" * 50_000
    msgs = [{"role": "user", "content": "p"}] + [
        {"role": "tool", "tool_call_id": f"c{i}", "content": big}
        for i in range(5)
    ]
    outer = cb.ContextBudgetConfig(
        max_input_tokens=200,  # absurdly tight -> refuse even after prune
        prune_at_pct=0.80,
        pad_pct=0.0,
        keep_recent=3,
        checkpoints_dir=tmp_path / "checkpoints",
    )
    with cb.with_config(outer):
        with cb.dispatch_context(
            budget_role="producer",
            runner_role="drafter",
            model="gpt-4o-mini",
            project_code="RSW",
            run_id="r4",
            audit_path=audit,
        ):
            try:
                cb.check_and_compress(
                    msgs, model="gpt-4o-mini", call_id="iter-0",
                )
            except cb.RecoverableContextError:
                pass
            else:  # pragma: no cover - the cap is tight enough to refuse
                raise AssertionError("expected RecoverableContextError")
    rows = _read_ctx_rows(audit)
    refusal = [r for r in rows if r.get("refusal_fired")]
    assert refusal, "no refusal row emitted"
    assert refusal[-1]["post_compression_tokens"] is not None
