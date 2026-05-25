"""A/B verdict-quality harness — 

The fourth piece staked out: **measure whether the alpha
sequence (per-role budgets / sparse
inboxes / .5 goal-state compression) actually buys real verdict
quality before promoting to beta.1**. Each prior alpha shipped a
plausibly-better-than-baseline change; none had been *measured*.

Alpha.6's job: run the same project objective twice with one knob
varied (compression-on vs compression-off this slice), capture
audit-row deltas, produce a comparison report. Once we trust the
harness, every later config decision routes through the same
measurement surface.

**Authority.** The harness READS existing audit-row surfaces. It
does NOT add new engine telemetry. The one experiment-local
provenance row (``actor='ab_harness'``) lives in the EXPERIMENT'S
audit log under ``<project>/experiments/<id>/audit.jsonl``, never
in the engine's run-level audit. Engine run audit is untouched.

**Two modes.**

  - **Stub mode** — default for tests + smoke + CI. Hand-crafted
    Leader / Producer / QC runners injected via the existing
    ``start_execution(kickoff_callable=, reflect_runner=)`` seams.
    One replicate per arm, deterministic. Proves wiring + report
    rendering.
  - **Live mode** — real configured runners, N replicates per arm
    (default N=3), variance aggregation, the actual measurement
    use case. Requires explicit ``allow_paid_live=True`` opt-in
    (raises :class:`ABHarnessUnconfirmedLiveCostError` otherwise).

**Variance posture.** The harness reports `directional_signal`
(absolute delta > 2 × combined stdev) with `statistical_test: null`
on every metric delta. No formal significance testing — N=3 doesn't
support it, and over-claiming significance from underpowered runs
is worse than under-claiming. The rendered markdown opens with an
explicit interpretation note (M2 — see :data:`INTERPRETATION_NOTE`).

**Replicate order.** Default interleaved A1, B1, A2, B2, A3, B3 to
reduce hidden confounds from provider drift / server-side load /
local machine state. ``ABConfig.order_seed`` supports deterministic
shuffle with the seed stamped into ``config.json``.

**Failure handling.** Per-replicate failure (LLM error, runtime
exception, timeout) logs ``success=False`` + ``error=<str>`` on the
``ReplicateManifest`` and does NOT abort the experiment. Other
replicates continue. Aggregation handles partial-success via
``n_successful`` / ``n_attempted``.

**Out of scope this slice** (documented in
``memory/project_modulatio_v22_deferred_jobs.md``):

  - **LLM judge for artifact-quality scoring** → (Job 3).
  - **``modulatio experiment`` CLI subcommand** → 0 polish
    (Job 4).
  - **Auto-tuning / closed-loop optimization** → future research
    arc (Job 5).
  - **UI surfacing of experiment results** → x polish (Job 6).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_logger = logging.getLogger("modulatio.ab_harness")


#: Required interpretation note on every :class:`ExperimentReport`.
#: M2 close-out (Lovecraft Round 1 design review): the rendered
#: markdown opens with this note in a fenced quote block BEFORE any
#: metric tables, deltas, or directional flags. ``directional_signal``
#: is a coarse "signal vs noise" indicator, NOT a statistical test;
#: this disclaimer protects the "no over-claiming" principle that
#: has guided the alpha sequence.
INTERPRETATION_NOTE = (
    "This experiment uses a directional signal rule only. "
    "No formal statistical test was performed. Results are "
    "exploratory and should be interpreted as such."
)


#: Closed enum of experiment-local audit row event types. Lives at
#: ``<experiment_id>/audit.jsonl`` under ``actor='ab_harness'``.
#: Engine run audit is NOT touched — these are EXPERIMENT provenance
#: rows, not engine telemetry.
AB_HARNESS_EVENT_LITERALS = (
    "experiment_started",
    "replicate_started",
    "replicate_completed",
    "replicate_failed",
    "experiment_completed",
)


#: Closed enum of varied dimensions. Alpha.6 scope is just compression;
#: this enum grows as later slices add inboxes / budgets / model
#: choice as A/B dimensions. Adding a value is a deliberate CHANGELOG
#: entry, never free text.
VARIED_DIMENSION_LITERALS = (
    "compression",
)


#: Closed enum of harness modes.
MODE_LITERALS = (
    "stub",
    "live",
)


# ─── Exceptions ──────────────────────────────────────────────────────────


class ABHarnessError(Exception):
    """Base for ab_harness-API failures."""


class ABHarnessUnconfirmedLiveCostError(ABHarnessError):
    """Raised by :func:`run_experiment` when ``mode="live"`` is set
    without the explicit ``allow_paid_live=True`` opt-in. The Python
    API is non-interactive; callers must explicitly acknowledge the
    cost of paid live runs (Nemo Round-1 disposition #3)."""


class ABHarnessInvalidConfigError(ABHarnessError):
    """Raised when an :class:`ABConfig` carries values outside the
    closed enums (varied_dimension / mode) or otherwise fails
    structural validation."""


# ─── Records ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ABConfig:
    """One A/B experiment's configuration. Immutable after
    construction; the same instance threads through estimate +
    run + report so the experiment surfaces a single canonical
    config_hash for forensic re-read.

    ``arm_a_value`` and ``arm_b_value`` are the values applied to the
    ``varied_dimension`` knob. For ``varied_dimension="compression"``,
    they're bools (True = compression on, False =
    free-markdown). Future dimensions may use different types.
    """

    base_project_config: dict
    varied_dimension: str
    arm_a_value: Any
    arm_b_value: Any
    replicates: int = 3
    mode: str = "stub"
    order_seed: int | None = None


@dataclass(frozen=True)
class CostEstimate:
    """Returned by :func:`estimate_experiment` for the dry-run path.
    Also surfaced on the final :class:`ExperimentReport` as
    ``cost_estimate_pre_flight`` so the user can compare the estimate
    against the actual total."""

    tokens_input_est: int
    tokens_output_est: int
    cost_usd_est: float
    wall_clock_est_seconds: float | None
    per_provider: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplicateManifest:
    """Per-replicate provenance. Lands at
    ``<experiment_id>/<arm>/<NNN>/manifest.json``.

    Carries POINTERS to the replicate's run_dir + audit_path + usage
    log + plan rather than copies — keeps experiment artifacts
    compact, and ``vault.run_dir(...)`` remains the source of truth
    for replicate-internal state. Forensic re-read follows the
    pointers.
    """

    arm: str
    replicate_index: int
    project_code: str
    run_id: str
    run_dir: str
    audit_path: str
    plan_id: str | None
    plan_final_status: str | None
    usage_log_path: str | None
    execution_result: dict
    effective_config: dict
    config_hash: str
    status: str
    started_at: str
    finished_at: str | None
    duration_seconds: float | None
    error: str | None = None


@dataclass(frozen=True)
class MetricSnapshot:
    """Per-replicate metric extract, read from existing audit rows +
    plan-local usage log. **No new telemetry** — / .4 / .5
    already write everything this struct consumes.

    Two compaction-skip count surfaces (Nemo Round-2 disposition on
    ``disabled_by_config``):

      - ``compaction_skip_counts`` carries ALL skip reasons including
        ``disabled_by_config`` (which fires once per **successfully
        parsed** Leader-reflect turn when
        ``Project.compression_enabled=False`` — parse-failed turns
        return on the pause path before the disabled-by-config
        emission, so the count tracks parseable reflections, not
        raw Leader-reflect invocations).
      - ``compaction_problem_skip_counts`` excludes
        ``disabled_by_config`` — the primary quality signal when
        comparing arms. ``disabled_by_config`` is forensic
        provenance, not a quality problem.
    """

    execution_success_rate: float | None
    first_pass_qc_accept_rate: float | None
    qc_reject_retry_count: int
    #: QC-as-fixer Slice 3: tasks completed via a QC-AUTHORED fix
    #: (verifier_result == "qc_authored_fix"). A SEPARATE bucket — these
    #: are controlled degradation, NEVER counted as clean producer wins or
    #: first-pass QC accepts. ``qc_authored_fix_unverified_count`` tracks
    #: QC fixes that failed independent verification (third-agent reject or
    #: none available) and were sent to human/Leader approval.
    qc_authored_fix_count: int
    qc_authored_fix_unverified_count: int
    divergence_note_count: int
    revise_minor_count: int
    revise_major_count: int
    first_pass_completion: bool
    failure_taxonomy: dict[str, int]
    context_budget_overcap_count: int
    context_budget_inband_count: int
    compaction_skip_counts: dict[str, int]
    compaction_problem_skip_counts: dict[str, int]
    propose_emit_count: int
    propose_accept_count: int
    propose_accept_ratio: float | None
    durable_note_read_count: int
    tokens_total: int
    cost_usd: float
    wall_clock_seconds: float


@dataclass(frozen=True)
class ArmAggregate:
    """Aggregated metrics across all successful replicates in one arm.
    Carries mean + stdev per metric; per-replicate breakdowns are
    preserved on disk via :attr:`per_replicate` paths so anyone
    wanting real statistics can re-derive them.

    ``metric_observation_counts`` (Nemo Medium 2 close-out,
    2026-05-19): per-metric count of non-``None`` observations actually
    used in the mean / stdev. A metric whose observations had None on
    some replicates will have a smaller count than ``n_successful``;
    this is what populates :attr:`MetricDelta.sample_sizes` so the
    delta carries honest per-metric counts rather than arm-level
    replicate counts.
    """

    arm: str
    n_successful: int
    n_attempted: int
    underpowered: bool
    metrics_mean: dict[str, float | None]
    metrics_stdev: dict[str, float | None]
    metric_observation_counts: dict[str, int]
    per_replicate: list[str]


@dataclass(frozen=True)
class MetricDelta:
    """Per-metric arm-A-minus-arm-B comparison.

    ``directional_signal`` is a coarse "signal vs noise" indicator
    (true iff ``abs(delta_mean) > 2 * combined_stdev``). It is
    EXPLICITLY NOT a statistical test — ``statistical_test`` is
    always ``None`` to make that clear at the data layer too.

    Guards (Nemo Round-1 + Round-2):

      - ``directional_signal=False`` if either arm is underpowered.
      - ``directional_signal=False`` if either arm has fewer than 2
        successful numeric observations for the metric.
      - ``directional_signal=False`` if either side carries ``None``.
      - Sign convention: ``arm_a - arm_b``, always.

    ``sample_sizes`` (Nemo Medium 2 close-out, 2026-05-19): per-metric
    non-``None`` observation counts ``(n_a, n_b)``. NOT arm-level
    ``n_successful`` — a metric whose snapshots had ``None`` on
    some replicates will report a smaller per-metric N than the
    arm's overall successful-replicate count. This is what downstream
    consumers should trust when reasoning about the signal flag.
    """

    delta_mean: float | None
    combined_stdev: float | None
    signal_rule: str
    directional_signal: bool
    statistical_test: None
    sample_sizes: tuple[int, int]


@dataclass(frozen=True)
class ExperimentReport:
    """The top-level experiment artifact. Materialized at
    ``<experiment_id>/report.json`` and rendered to
    ``<experiment_id>/report.md`` via :func:`render_report_markdown`.

    ``interpretation_note`` is REQUIRED (M2 close-out, Lovecraft
    Round 1). The rendered markdown opens with the note before any
    metric tables. Callers cannot omit it; the harness builder
    populates it from :data:`INTERPRETATION_NOTE`.
    """

    experiment_id: str
    config: ABConfig
    config_hash: str
    arm_a: ArmAggregate
    arm_b: ArmAggregate
    deltas: dict[str, MetricDelta]
    total_cost_usd: float
    cost_estimate_pre_flight: CostEstimate
    started_at: str
    finished_at: str
    replicate_order: list[tuple[str, int]]
    interpretation_note: str


# ─── Metric flattening ───────────────────────────────────────────────────


#: Scalar metric field names on :class:`MetricSnapshot` — aggregated
#: across replicates as mean + stdev (None values excluded from
#: aggregation).
_SCALAR_METRIC_FIELDS = (
    "execution_success_rate",
    "first_pass_qc_accept_rate",
    "qc_reject_retry_count",
    "qc_authored_fix_count",
    "qc_authored_fix_unverified_count",
    "divergence_note_count",
    "revise_minor_count",
    "revise_major_count",
    "context_budget_overcap_count",
    "context_budget_inband_count",
    "propose_emit_count",
    "propose_accept_count",
    "propose_accept_ratio",
    "durable_note_read_count",
    "tokens_total",
    "cost_usd",
    "wall_clock_seconds",
)


def _flatten_snapshot(snap: MetricSnapshot) -> dict[str, float | None]:
    """Project a :class:`MetricSnapshot` into the scalar metric space
    used for aggregation + deltas. Bool ``first_pass_completion``
    becomes 0.0/1.0 so per-arm "fraction first-pass" naturally falls
    out as mean. Dict-valued fields are collapsed to totals
    (``*_total`` keys) so the comparison stays a flat scalar table;
    the underlying dicts are preserved per-replicate on disk for
    forensic re-read."""
    flat: dict[str, float | None] = {}
    for field_name in _SCALAR_METRIC_FIELDS:
        value = getattr(snap, field_name)
        if value is None:
            flat[field_name] = None
        else:
            flat[field_name] = float(value)
    # Boolean → 0/1 (per-arm mean becomes "fraction first-pass")
    flat["first_pass_completion_rate"] = 1.0 if snap.first_pass_completion else 0.0
    # Dict → total (compaction + failure rollups). Per-key
    # breakdowns are preserved in MetricSnapshot dicts on disk.
    flat["compaction_skips_total"] = float(sum(snap.compaction_skip_counts.values()))
    flat["compaction_problem_skips_total"] = float(
        sum(snap.compaction_problem_skip_counts.values())
    )
    flat["failure_total"] = float(sum(snap.failure_taxonomy.values()))
    return flat


# ─── Aggregation: ArmAggregate + MetricDelta ─────────────────────────────


def aggregate_arm(
    *,
    arm: str,
    snapshots: list[MetricSnapshot],
    per_replicate_paths: list[str],
    n_attempted: int,
) -> ArmAggregate:
    """Aggregate per-replicate :class:`MetricSnapshot` values into one
    :class:`ArmAggregate`. None values are EXCLUDED from per-metric
    mean/stdev (a replicate that crashed before producing a numeric
    observation for a metric doesn't drag the mean down).

    ``n_attempted`` may exceed ``len(snapshots)`` when some replicates
    failed entirely (no snapshot produced). ``n_successful`` is
    derived from ``len(snapshots)``.

    ``underpowered=True`` iff ``n_successful < 2``."""
    n_successful = len(snapshots)
    underpowered = n_successful < 2

    flat_per_replicate = [_flatten_snapshot(s) for s in snapshots]
    all_metric_names: set[str] = set()
    for f in flat_per_replicate:
        all_metric_names.update(f.keys())

    metrics_mean: dict[str, float | None] = {}
    metrics_stdev: dict[str, float | None] = {}
    metric_observation_counts: dict[str, int] = {}
    for name in sorted(all_metric_names):
        observations = [
            f.get(name) for f in flat_per_replicate
            if f.get(name) is not None
        ]
        # Nemo Medium 2 (2026-05-19): record the per-metric N even
        # when None / empty, so downstream readers can tell
        # "no observations" (count=0) from "didn't aggregate".
        metric_observation_counts[name] = len(observations)
        if not observations:
            metrics_mean[name] = None
            metrics_stdev[name] = None
            continue
        mean = sum(observations) / len(observations)
        if len(observations) < 2:
            stdev: float | None = None
        else:
            # Sample stdev (Bessel-corrected). N=1 returns None above.
            var = sum((x - mean) ** 2 for x in observations) / (len(observations) - 1)
            stdev = var ** 0.5
        metrics_mean[name] = mean
        metrics_stdev[name] = stdev

    return ArmAggregate(
        arm=arm,
        n_successful=n_successful,
        n_attempted=n_attempted,
        underpowered=underpowered,
        metrics_mean=metrics_mean,
        metrics_stdev=metrics_stdev,
        metric_observation_counts=metric_observation_counts,
        per_replicate=list(per_replicate_paths),
    )


#: Fixed signal rule string surfaced on every :class:`MetricDelta`.
#: Kept as a string so a forensic reader can re-derive the
#: directional_signal flag without re-reading the harness source.
#:
#: NOTE on the zero-stdev case (Nemo close-out, 2026-05-19): when
#: ``combined_stdev == 0`` AND both arms have ≥2 observations each,
#: the rule fires for ANY nonzero delta — a deterministic arm
#: difference is the cleanest possible directional signal, not a
#: degenerate case. The signal_rule string covers this by literal
#: math (``abs(delta_mean) > 2 * 0`` ↔ ``abs(delta_mean) > 0``).
_SIGNAL_RULE = "abs(delta_mean) > 2 * combined_stdev"


def compute_deltas(
    *,
    arm_a: ArmAggregate,
    arm_b: ArmAggregate,
) -> dict[str, MetricDelta]:
    """Return a dict of metric_name → :class:`MetricDelta`. Sign
    convention is ``arm_a - arm_b`` always.

    ``directional_signal`` is False when:
      - Either arm is underpowered.
      - Either arm's mean for the metric is None.
      - Either arm's per-metric observation count was < 2 (so its
        stdev is unknown).
      - The signal rule itself doesn't trip
        (``abs(delta_mean) <= 2 * combined_stdev``).

    ``statistical_test`` is always ``None`` — the harness explicitly
    does NOT perform formal significance testing (M2 + Nemo Round-1
    posture). The directional flag is a coarse signal-vs-noise
    indicator, nothing more."""
    deltas: dict[str, MetricDelta] = {}
    metric_names = sorted(
        set(arm_a.metrics_mean.keys()) | set(arm_b.metrics_mean.keys())
    )
    for name in metric_names:
        mean_a = arm_a.metrics_mean.get(name)
        mean_b = arm_b.metrics_mean.get(name)
        stdev_a = arm_a.metrics_stdev.get(name)
        stdev_b = arm_b.metrics_stdev.get(name)

        if mean_a is None or mean_b is None:
            deltas[name] = MetricDelta(
                delta_mean=None,
                combined_stdev=None,
                signal_rule=_SIGNAL_RULE,
                directional_signal=False,
                statistical_test=None,
                sample_sizes=(
                    arm_a.metric_observation_counts.get(name, 0),
                    arm_b.metric_observation_counts.get(name, 0),
                ),
            )
            continue

        delta_mean = mean_a - mean_b

        if stdev_a is None or stdev_b is None:
            # Either side had only 1 observation for the metric →
            # cannot compute combined stdev → cannot fire signal flag.
            deltas[name] = MetricDelta(
                delta_mean=delta_mean,
                combined_stdev=None,
                signal_rule=_SIGNAL_RULE,
                directional_signal=False,
                statistical_test=None,
                sample_sizes=(
                    arm_a.metric_observation_counts.get(name, 0),
                    arm_b.metric_observation_counts.get(name, 0),
                ),
            )
            continue

        # Pooled stdev under independent-arm assumption. NOT a
        # significance test, just a coarse noise floor for the
        # directional_signal flag.
        combined_stdev = (stdev_a ** 2 + stdev_b ** 2) ** 0.5
        # Underpower guard (Nemo Round-1 + Round-2): never fire flag
        # if either arm is underpowered as a whole, regardless of
        # per-metric obs count.
        if arm_a.underpowered or arm_b.underpowered:
            directional = False
        else:
            # Nemo close-out (2026-05-19): NO extra ``combined_stdev > 0``
            # guard. When both arms are internally consistent and differ
            # from each other, ``combined_stdev == 0`` and ANY nonzero
            # delta is the cleanest possible directional signal. Stub
            # mode in particular produces zero-variance arms by design;
            # the guard was silently dropping every deterministic signal.
            directional = abs(delta_mean) > 2 * combined_stdev
        deltas[name] = MetricDelta(
            delta_mean=delta_mean,
            combined_stdev=combined_stdev,
            signal_rule=_SIGNAL_RULE,
            directional_signal=directional,
            statistical_test=None,
            sample_sizes=(
                arm_a.metric_observation_counts.get(name, 0),
                arm_b.metric_observation_counts.get(name, 0),
            ),
        )

    return deltas


# ─── Replicate order ─────────────────────────────────────────────────────


def _replicate_order(
    *,
    replicates: int,
    order_seed: int | None,
) -> list[tuple[str, int]]:
    """Compute the replicate execution order.

    Default: interleaved A1, B1, A2, B2, A3, B3 — reduces hidden
    confounds from provider drift / server-side load / local
    machine state (Nemo Round-1 #5).

    With ``order_seed``: deterministic Fisher-Yates shuffle seeded
    with the given int. The seed is stamped onto the experiment's
    audit + report so the order is reproducible."""
    base = []
    for i in range(1, replicates + 1):
        base.append(("a", i))
        base.append(("b", i))
    if order_seed is None:
        return base
    import random
    rng = random.Random(order_seed)
    shuffled = list(base)
    rng.shuffle(shuffled)
    return shuffled


# ─── Stub-mode runner protocol ───────────────────────────────────────────


# Type alias for the replicate_factory callback. Returns a tuple of:
#   (project, runners, kickoff_callable, plan_record_or_None,
#    reflect_runner_or_None, expected_usage_log_path_or_None)
# Each replicate calls the factory to get a fresh Project (so arm
# overrides don't leak across replicates) plus the injection points
# `project_execution.start_execution` consumes. The factory owns
# vault.init_project + vault.init_run for each replicate.
#
# Mode-agnostic: the SAME shape works for stub mode (hand-crafted
# runners) and live mode (real LLM-backed runners). The factory is
# the only seam where the caller controls whether the replicate
# uses fakes or actually spends money. Determinism controls
# (temperature=0, seed propagation) live in the caller's runner
# construction — the harness cannot enforce them through this
# interface.
#
# Returning the expected usage_log_path lets the harness find the
# plan-local <plan_id>.usage.jsonl after execution (needed by
# extract_metric_snapshot). None if the caller doesn't want to
# track usage.

ReplicateFactory = Any  # Callable[[str, int, dict], tuple]


# ─── Cost estimation ─────────────────────────────────────────────────────


#: Coarse per-replicate token budget used by the cost estimator
#: when no per-config history is available. Matches the rough
#: shape of a small project execution: ~15k input tokens + ~5k
#: output tokens across all LLM calls in a single replicate.
#: Deliberately conservative (high) — the goal of the estimate is
#: to keep the user from being surprised, not to be accurate.
_DEFAULT_TOKENS_PER_REPLICATE_INPUT = 15_000
_DEFAULT_TOKENS_PER_REPLICATE_OUTPUT = 5_000


#: Per-provider USD-per-million-token pricing. Keyed by lowercase
#: model prefix; closest prefix wins. Values are conservative
#: rounded estimates as of — refresh these when prices
#: shift materially. Unknown models default to the highest tier
#: in the table to keep the estimate's "no surprise" promise.
#:
#: Format: ``"<prefix>": (input_per_million, output_per_million)``.
_PRICING_USD_PER_M_TOKENS = {
    "anthropic/claude-opus": (15.0, 75.0),
    "anthropic/claude-sonnet": (3.0, 15.0),
    "anthropic/claude-haiku": (1.0, 5.0),
    "openai/gpt-5": (5.0, 20.0),
    "openai/gpt-4": (2.5, 10.0),
    "google/gemini-2.5": (1.25, 5.0),
    "google/gemini-2": (0.5, 2.0),
    "xai/grok-4": (3.0, 15.0),
    "openrouter/": (3.0, 15.0),
    "stub": (0.0, 0.0),
}


def _pricing_for_model(model: str | None) -> tuple[float, float]:
    """Return (input_per_million, output_per_million) USD for a
    model identifier. Unknown / None defaults to the most expensive
    tier (so the estimate never under-promises)."""
    if not model:
        return (15.0, 75.0)
    needle = model.lower()
    # Pick the longest matching prefix
    matches = [
        (prefix, pricing)
        for prefix, pricing in _PRICING_USD_PER_M_TOKENS.items()
        if needle.startswith(prefix)
    ]
    if not matches:
        return (15.0, 75.0)
    matches.sort(key=lambda pair: len(pair[0]), reverse=True)
    return matches[0][1]


def estimate_experiment(config: ABConfig) -> CostEstimate:
    """Pre-flight token + cost estimate for an :class:`ABConfig`.
    Returns ``CostEstimate(0, 0, 0.0, None)`` for ``mode="stub"``
    (deterministic, no LLM calls). For live mode, multiplies a
    conservative per-replicate token budget by ``2 * replicates``
    and applies the per-provider rate.

    ``wall_clock_est_seconds`` is always ``None`` from this function
    — the harness updates it in-place on the report after the first
    replicate completes (calibrated against observed duration).

    This estimator is deliberately simple: token-count ×
    per-provider-pricing-table, no cache-discount modeling, no
    dynamic-pricing fetches, no batch-pricing. Anything beyond that
    is M1-tripwire territory (split to.1). The estimator's
    job is to keep users from being surprised by paid runs, not to
    predict cost within 10%.
    """
    if config.mode == "stub":
        return CostEstimate(
            tokens_input_est=0,
            tokens_output_est=0,
            cost_usd_est=0.0,
            wall_clock_est_seconds=None,
        )

    # Live mode: paper math
    model = config.base_project_config.get("leader_model")
    in_rate_per_m, out_rate_per_m = _pricing_for_model(model)

    total_replicates = config.replicates * 2  # both arms
    tokens_in = _DEFAULT_TOKENS_PER_REPLICATE_INPUT * total_replicates
    tokens_out = _DEFAULT_TOKENS_PER_REPLICATE_OUTPUT * total_replicates

    cost_usd_est = (
        (tokens_in / 1_000_000) * in_rate_per_m
        + (tokens_out / 1_000_000) * out_rate_per_m
    )

    per_provider = {
        "default": {
            "model": model or "(unknown)",
            "input_rate_per_million_usd": in_rate_per_m,
            "output_rate_per_million_usd": out_rate_per_m,
            "tokens_input_est": tokens_in,
            "tokens_output_est": tokens_out,
            "cost_usd_est": cost_usd_est,
        },
    }

    return CostEstimate(
        tokens_input_est=tokens_in,
        tokens_output_est=tokens_out,
        cost_usd_est=cost_usd_est,
        wall_clock_est_seconds=None,
        per_provider=per_provider,
    )


# ─── Experiment-local audit emitter (actor='ab_harness') ─────────────────


def _utcnow_iso() -> str:
    """ISO8601 UTC timestamp matching the/.5 audit-row shape."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_ab_audit_row(audit_path: Path, row: dict) -> None:
    """Append one experiment-local audit row. Best-effort — write
    failures log WARN and return (mirrors the inbox +
    compression audit posture: the experiment must not be broken by
    an audit-write hiccup).

    File mode 0o600; parent dir 0o700. ``ensure_ascii=True`` so
    future field values whose ``str()`` returns multiline content
    can't smuggle bytes that break the JSONL contract."""
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not audit_path.exists():
            try:
                audit_path.touch(mode=0o600, exist_ok=True)
            except OSError:
                pass
        try:
            audit_path.chmod(0o600)
        except OSError:
            pass
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str, ensure_ascii=True) + "\n")
    except Exception as exc:  # noqa: BLE001 — best-effort audit
        _logger.warning(
            "ab_harness audit append failed at %s: %s", audit_path, exc,
        )


def _write_artifact_0600(path: Path, text: str) -> None:
    """Write an experiment artifact (config / manifest / metrics /
    aggregate / report) with mode ``0o600`` and parent dir ``0o700``.

    Nemo Low close-out (2026-05-19): the audit file was hardened but
    the rest of the experiment surface inherited process umask, which
    could expose effective config, run paths, plan ids, and cost
    estimates to other local users on a shared box. Apply the same
    posture used by the compression module so the harness's
    forensic-traceability artifacts get the same protection as the
    audit log.

    Not strictly atomic (no temp-file-then-replace dance) — the
    harness writes each artifact exactly once per experiment, so a
    half-written ``config.json`` after a crash would land an
    obviously-invalid file that the next reader rejects. Crucially,
    artifacts are write-once-per-experiment: a crash-mangled file is
    replaced by the next explicit ``run_experiment`` invocation, never
    silently repaired in place, so the absence of a temp-replace dance
    can't leave a stale-but-plausible artifact masquerading as good.
    Use a create-with-mode-then-chmod-belt-and-suspenders pattern."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        # Create file with restrictive mode from the start (no umask
        # window): O_CREAT|O_TRUNC|O_WRONLY at 0o600.
        import os as _os
        fd = _os.open(
            str(path),
            _os.O_CREAT | _os.O_TRUNC | _os.O_WRONLY,
            0o600,
        )
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            try:
                _os.close(fd)
            except OSError:
                pass
            raise
    except OSError:
        # Windows / read-only mounts / odd POSIX behavior — fall back
        # to ``write_text`` so the experiment still completes.
        path.write_text(text, encoding="utf-8")
    # Belt-and-suspenders: chmod after write in case the platform's
    # O_CREAT didn't honor the mode (some Windows / WSL combos).
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _base_ab_audit_row(
    *,
    event: str,
    experiment_id: str,
    varied_dimension: str,
    config_hash: str,
) -> dict:
    """Return the base audit row with every nullable field present
    as ``None`` per the join contract (mirrored here for
    experiment-local rows). Per-event overlays layer specific fields
    on top — never omit a key from the base schema."""
    return {
        "timestamp": _utcnow_iso(),
        "actor": "ab_harness",
        "event": event,
        "experiment_id": experiment_id,
        "arm": None,
        "replicate_index": None,
        "varied_dimension": varied_dimension,
        "effective_arm_value": None,
        "project_code": None,
        "run_id": None,
        "run_dir": None,
        "audit_path": None,
        "status": None,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "error": None,
        "config_hash": config_hash,
    }


def emit_experiment_started(
    *,
    audit_path: Path,
    experiment_id: str,
    varied_dimension: str,
    config_hash: str,
    started_at: str,
) -> None:
    """Append the ``experiment_started`` row. One per experiment.
    ``arm`` / ``replicate_index`` / per-replicate fields are
    ``None`` — this is an experiment-level marker."""
    row = _base_ab_audit_row(
        event="experiment_started",
        experiment_id=experiment_id,
        varied_dimension=varied_dimension,
        config_hash=config_hash,
    )
    row["status"] = "started"
    row["started_at"] = started_at
    _append_ab_audit_row(audit_path, row)


def emit_experiment_completed(
    *,
    audit_path: Path,
    experiment_id: str,
    varied_dimension: str,
    config_hash: str,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    total_cost_usd: float,
) -> None:
    """Append the ``experiment_completed`` row. One per experiment.
    Carries total cost + duration so the experiment-level wrap is
    self-contained."""
    row = _base_ab_audit_row(
        event="experiment_completed",
        experiment_id=experiment_id,
        varied_dimension=varied_dimension,
        config_hash=config_hash,
    )
    row["status"] = "completed"
    row["started_at"] = started_at
    row["finished_at"] = finished_at
    row["duration_seconds"] = duration_seconds
    row["total_cost_usd"] = total_cost_usd
    _append_ab_audit_row(audit_path, row)


def emit_replicate_started(
    *,
    audit_path: Path,
    experiment_id: str,
    varied_dimension: str,
    config_hash: str,
    arm: str,
    replicate_index: int,
    effective_arm_value: Any,
    project_code: str,
    run_id: str,
    run_dir: str,
    audit_path_engine: str,
    started_at: str,
) -> None:
    """Append the per-replicate ``replicate_started`` row."""
    row = _base_ab_audit_row(
        event="replicate_started",
        experiment_id=experiment_id,
        varied_dimension=varied_dimension,
        config_hash=config_hash,
    )
    row["arm"] = arm
    row["replicate_index"] = replicate_index
    row["effective_arm_value"] = effective_arm_value
    row["project_code"] = project_code
    row["run_id"] = run_id
    row["run_dir"] = run_dir
    row["audit_path"] = audit_path_engine
    row["status"] = "started"
    row["started_at"] = started_at
    _append_ab_audit_row(audit_path, row)


def emit_replicate_completed(
    *,
    audit_path: Path,
    experiment_id: str,
    varied_dimension: str,
    config_hash: str,
    arm: str,
    replicate_index: int,
    effective_arm_value: Any,
    project_code: str,
    run_id: str,
    run_dir: str,
    audit_path_engine: str,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
) -> None:
    """Append the per-replicate ``replicate_completed`` row."""
    row = _base_ab_audit_row(
        event="replicate_completed",
        experiment_id=experiment_id,
        varied_dimension=varied_dimension,
        config_hash=config_hash,
    )
    row["arm"] = arm
    row["replicate_index"] = replicate_index
    row["effective_arm_value"] = effective_arm_value
    row["project_code"] = project_code
    row["run_id"] = run_id
    row["run_dir"] = run_dir
    row["audit_path"] = audit_path_engine
    row["status"] = "completed"
    row["started_at"] = started_at
    row["finished_at"] = finished_at
    row["duration_seconds"] = duration_seconds
    _append_ab_audit_row(audit_path, row)


def emit_replicate_failed(
    *,
    audit_path: Path,
    experiment_id: str,
    varied_dimension: str,
    config_hash: str,
    arm: str,
    replicate_index: int,
    effective_arm_value: Any,
    project_code: str | None,
    run_id: str | None,
    run_dir: str | None,
    audit_path_engine: str | None,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    error: str,
) -> None:
    """Append the per-replicate ``replicate_failed`` row. ``error``
    is required; ``project_code`` / ``run_id`` / paths may be
    ``None`` if the replicate crashed before ``vault.init_run`` ran."""
    row = _base_ab_audit_row(
        event="replicate_failed",
        experiment_id=experiment_id,
        varied_dimension=varied_dimension,
        config_hash=config_hash,
    )
    row["arm"] = arm
    row["replicate_index"] = replicate_index
    row["effective_arm_value"] = effective_arm_value
    row["project_code"] = project_code
    row["run_id"] = run_id
    row["run_dir"] = run_dir
    row["audit_path"] = audit_path_engine
    row["status"] = "failed"
    row["started_at"] = started_at
    row["finished_at"] = finished_at
    row["duration_seconds"] = duration_seconds
    row["error"] = error
    _append_ab_audit_row(audit_path, row)


# ─── Markdown report rendering ───────────────────────────────────────────


def _fmt_metric(value: float | None, precision: int = 4) -> str:
    """Render a metric scalar for the markdown table. None → ``n/a``."""
    if value is None:
        return "n/a"
    if abs(value) >= 1000:
        return f"{value:,.{precision}f}"
    return f"{value:.{precision}f}"


def _fmt_signal(delta: MetricDelta) -> str:
    """Render the directional-signal marker. Uses prose, not symbols,
    so a casual reader cannot mistake it for a significance test."""
    if delta.directional_signal:
        return "**directional signal**"
    return "noise-bound"


def render_report_markdown(report: ExperimentReport) -> str:
    """Render an :class:`ExperimentReport` as human-readable markdown.

    Opens with the :data:`INTERPRETATION_NOTE` in a fenced quote block
    BEFORE any metric tables, deltas, or directional flags — so a
    reader scanning top-down cannot miss the "directional, not
    statistical" disclaimer (M2 close-out, Lovecraft Round 1).

    Metric layout:
      - Headline quality row uses ``compaction_problem_skips_total``
        (excludes ``disabled_by_config`` — L2 close-out).
      - Per-arm detail table carries every scalar metric +
        directional signal flag.
      - Full ``compaction_skip_counts`` (including
        ``disabled_by_config``) appears in an expanded provenance
        section.
    """
    lines: list[str] = []
    cfg = report.config

    lines.append(f"# Experiment Report: {report.experiment_id}")
    lines.append("")

    # ── M2 interpretation note — MUST appear BEFORE any metric data ──
    lines.append("> **Interpretation note**")
    lines.append(">")
    for note_line in report.interpretation_note.split(". "):
        text = note_line.strip()
        if not text:
            continue
        if not text.endswith("."):
            text = text + "."
        lines.append(f"> {text}")
    lines.append("")

    # ── Experiment metadata ──
    lines.append("## Experiment")
    lines.append("")
    lines.append(f"- **Varied dimension:** `{cfg.varied_dimension}`")
    lines.append(f"- **Mode:** `{cfg.mode}`")
    lines.append(f"- **Replicates per arm:** {cfg.replicates}")
    lines.append(f"- **Arm A value:** `{cfg.arm_a_value!r}`")
    lines.append(f"- **Arm B value:** `{cfg.arm_b_value!r}`")
    if cfg.order_seed is not None:
        lines.append(f"- **Order seed:** `{cfg.order_seed}`")
    else:
        lines.append("- **Order:** interleaved (A1, B1, A2, B2, ...)")
    lines.append(f"- **Started:** `{report.started_at}`")
    lines.append(f"- **Finished:** `{report.finished_at}`")
    lines.append(f"- **Config hash:** `{report.config_hash[:16]}...`")
    lines.append(f"- **Total cost (USD):** ${report.total_cost_usd:.4f}")
    if report.cost_estimate_pre_flight.cost_usd_est > 0:
        lines.append(
            f"- **Pre-flight estimate (USD):** "
            f"${report.cost_estimate_pre_flight.cost_usd_est:.4f}"
        )
    lines.append("")

    # ── Arm summary ──
    lines.append("## Arm summary")
    lines.append("")
    lines.append("| Arm | Successful / attempted | Underpowered |")
    lines.append("|---|---|---|")
    for arm in (report.arm_a, report.arm_b):
        lines.append(
            f"| {arm.arm.upper()} | "
            f"{arm.n_successful}/{arm.n_attempted} | "
            f"{'**yes**' if arm.underpowered else 'no'} |"
        )
    lines.append("")

    # ── Headline quality comparison ──
    lines.append("## Headline quality signals (arm A − arm B)")
    lines.append("")
    lines.append("Sign convention: ``delta_mean = arm_a − arm_b``. Positive "
                 "value ⇒ arm A's metric is higher.")
    lines.append("")
    lines.append("| Metric | Arm A mean | Arm B mean | Δ mean | Combined stdev | Direction |")
    lines.append("|---|---|---|---|---|---|")
    headline_keys = (
        "execution_success_rate",
        "first_pass_qc_accept_rate",
        "first_pass_completion_rate",
        "divergence_note_count",
        "compaction_problem_skips_total",
        "failure_total",
        "tokens_total",
        "cost_usd",
        "wall_clock_seconds",
    )
    for key in headline_keys:
        mean_a = report.arm_a.metrics_mean.get(key)
        mean_b = report.arm_b.metrics_mean.get(key)
        delta = report.deltas.get(key)
        if delta is None:
            continue
        lines.append(
            f"| `{key}` | {_fmt_metric(mean_a)} | {_fmt_metric(mean_b)} | "
            f"{_fmt_metric(delta.delta_mean)} | "
            f"{_fmt_metric(delta.combined_stdev)} | "
            f"{_fmt_signal(delta)} |"
        )
    lines.append("")

    # ── Full per-arm metric detail ──
    lines.append("## All metrics — per arm")
    lines.append("")
    all_keys = sorted(
        set(report.arm_a.metrics_mean.keys())
        | set(report.arm_b.metrics_mean.keys())
    )
    lines.append("| Metric | Arm A mean ± stdev | Arm B mean ± stdev | Δ mean | Direction |")
    lines.append("|---|---|---|---|---|")
    for key in all_keys:
        mean_a = report.arm_a.metrics_mean.get(key)
        stdev_a = report.arm_a.metrics_stdev.get(key)
        mean_b = report.arm_b.metrics_mean.get(key)
        stdev_b = report.arm_b.metrics_stdev.get(key)
        delta = report.deltas.get(key)
        a_str = f"{_fmt_metric(mean_a)} ± {_fmt_metric(stdev_a)}"
        b_str = f"{_fmt_metric(mean_b)} ± {_fmt_metric(stdev_b)}"
        d_str = _fmt_metric(delta.delta_mean) if delta else "—"
        dir_str = _fmt_signal(delta) if delta else "—"
        lines.append(f"| `{key}` | {a_str} | {b_str} | {d_str} | {dir_str} |")
    lines.append("")

    # ── Compaction provenance (full skip counts, includes disabled_by_config) ──
    lines.append("## Compaction skip provenance")
    lines.append("")
    lines.append(
        "`compaction_skips_total` includes all skip reasons; "
        "`compaction_problem_skips_total` (the quality signal in the "
        "headline) excludes `disabled_by_config`, which is forensic "
        "provenance for the compression-off arm rather than a quality "
        "issue."
    )
    lines.append("")

    # ── Sample sizes + signal rule footer ──
    lines.append("## Notes")
    lines.append("")
    lines.append(f"- **Signal rule:** `{_SIGNAL_RULE}`")
    lines.append(
        f"- **Sample sizes:** arm A = {report.arm_a.n_successful}, "
        f"arm B = {report.arm_b.n_successful} "
        f"(attempted: A = {report.arm_a.n_attempted}, "
        f"B = {report.arm_b.n_attempted})"
    )
    lines.append(
        "- **Statistical test:** none performed; `statistical_test` "
        "on every `MetricDelta` is explicitly `null`."
    )
    if cfg.order_seed is not None:
        lines.append(
            f"- **Replicate order seed:** `{cfg.order_seed}` (shuffled)"
        )
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ─── Config hashing ──────────────────────────────────────────────────────


def compute_config_hash(effective_config: dict) -> str:
    """Return a deterministic sha256 hex of an effective config dict.
    Used by :class:`ReplicateManifest` (and the experiment-level
    config_hash) so two byte-equal configs produce byte-equal hashes
    across runs, machines, Python versions.

    ``sort_keys=True`` + ``default=str`` handles non-JSON types
    deterministically (Path → str, datetime → str, etc.)."""
    payload = json.dumps(
        effective_config,
        sort_keys=True,
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ─── Audit row + usage log reading ───────────────────────────────────────


def _read_audit_rows(audit_path: Path) -> list[dict]:
    """Parse audit.jsonl into a list of dicts. Returns [] if file is
    absent or empty. Malformed lines are skipped with a warning —
    extraction must remain robust to partial corruption."""
    if not audit_path.exists():
        return []
    rows: list[dict] = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            _logger.warning(
                "skipping malformed audit line in %s: %s", audit_path, exc,
            )
    return rows


def _read_usage_rows(usage_log_path: Path | None) -> list[dict]:
    """Parse the plan-local usage log into a list of dicts. Same
    robustness posture as :func:`_read_audit_rows`. Returns [] when
    the path is None or absent."""
    if usage_log_path is None or not usage_log_path.exists():
        return []
    rows: list[dict] = []
    for line in usage_log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            _logger.warning(
                "skipping malformed usage line in %s: %s", usage_log_path, exc,
            )
    return rows


# ─── Per-replicate metric extraction ─────────────────────────────────────


#: Compaction skip reasons that count as "problem" skips for the
#: quality signal (Nemo Round-2 metric-split). Excludes
#: ``disabled_by_config`` because that's forensic provenance for the
#: compression-off arm, NOT a quality issue.
_COMPACTION_PROBLEM_SKIP_REASONS = (
    "missing_state_doc",
    "malformed_state_doc",
    "no_material_change",
)


def extract_metric_snapshot(
    *,
    audit_path: Path,
    usage_log_path: Path | None,
    reflection_log: list[dict] | None,
    execution_result: dict,
    wall_clock_seconds: float,
    tasks: list[Any] | None = None,
) -> MetricSnapshot:
    """Build a per-replicate :class:`MetricSnapshot` by reading only
    EXISTING surfaces — ``audit.jsonl`` for inbox / budget /
    compression rows, the plan-local usage log, ``ExecutionResult``,
    and (Nemo Medium 1 close-out, 2026-05-19) per-task
    ``Task.transitions`` for QC verdicts. No new telemetry —
    / / already write everything consumed here.

    Args:
      audit_path: ``<run>/audit.jsonl`` for the replicate.
      usage_log_path: ``<plan_id>.usage.jsonl`` (plan-local, beside
        the plan record). ``None`` if no plan ran or the log path
        is unknown.
      reflection_log: the plan's ``reflection_log`` list (each entry
        a dict with at least ``outcome`` + payload keys). ``None``
        if no plan ran.
      execution_result: ``ExecutionResult.__dict__`` snapshot from
        ``project_execution.start_execution``. Used to classify
        failure taxonomy + execution success.
      wall_clock_seconds: end-to-end replicate duration.
      tasks: list of :class:`Task` records for the replicate's run
        (``store.list_tasks(project_code, run_id=...)``). QC
        verdicts are read from ``Task.transitions`` (where the
        engine actually persists them) rather than from
        ``audit.jsonl`` (where they were never written). ``None``
        when no task store is available → QC metrics report as
        ``first_pass_qc_accept_rate=None`` / ``qc_reject_retry_count=0``
        and the QC failure bucket is empty.

    Returns:
      A frozen :class:`MetricSnapshot`.
    """
    audit_rows = _read_audit_rows(audit_path)
    usage_rows = _read_usage_rows(usage_log_path)
    reflection_log = reflection_log or []

    # ── QC verdicts: per-task first-verdict + retry-rejection counts ──
    # Nemo Medium 1 (2026-05-19): QC verdicts persist in
    # ``Task.transitions`` (engine source-of-truth), NOT
    # ``audit.jsonl``. The earlier ``actor=='qc'`` filter against
    # audit rows never matched, so QC metrics always reported
    # ``None`` / 0 in real runs.
    qc_transitions_by_task: dict[str, list[Any]] = _qc_transitions_by_task(tasks)
    first_pass_accepts = 0
    qc_reject_retry_count = 0
    for _, transitions in qc_transitions_by_task.items():
        # transitions are already in append order (engine writes them
        # one-at-a-time at QC decision points).
        first = transitions[0]
        if _qc_transition_passed(first):
            first_pass_accepts += 1
        for prior in transitions[:-1]:
            if not _qc_transition_passed(prior):
                qc_reject_retry_count += 1

    n_tasks_with_qc = len(qc_transitions_by_task)
    first_pass_qc_accept_rate = (
        first_pass_accepts / n_tasks_with_qc
        if n_tasks_with_qc > 0 else None
    )

    # ── QC-as-fixer Slice 3: count QC-authored fixes as a SEPARATE bucket.
    # These are NEVER clean producer wins (they're controlled degradation),
    # so they get their own counters rather than folding into the accept
    # rate. Read from the transition verifier_result literals.
    qc_authored_fix_count = 0
    qc_authored_fix_unverified_count = 0
    for t in tasks or []:
        for tr in getattr(t, "transitions", []) or []:
            vr = getattr(tr, "verifier_result", None)
            if vr == "qc_authored_fix":
                qc_authored_fix_count += 1
            elif vr == "qc_authored_fix_unverified":
                qc_authored_fix_unverified_count += 1

    # ── Execution success rate (plan-level) ──
    # ExecutionResult.final_status == "done" means the plan completed
    # all sub-objectives without pause/abort/failure. Use as a coarse
    # plan-level success indicator (renamed from "qc_pass_rate" per
    # Nemo Round-2 sharper definition).
    final_status = execution_result.get("final_status")
    execution_success_rate: float | None
    if final_status is None:
        execution_success_rate = None
    elif final_status == "done":
        execution_success_rate = 1.0
    else:
        execution_success_rate = 0.0

    # ── Divergence notes (project_execution custom audit entries) ──
    divergence_note_count = sum(
        1 for r in audit_rows
        if r.get("kind") == "team_state_divergence"
    )

    # ── Reflection-log outcomes (revise-minor / revise-major counts) ──
    revise_minor_count = sum(
        1 for entry in reflection_log
        if entry.get("outcome") == "revise-minor"
    )
    revise_major_count = sum(
        1 for entry in reflection_log
        if entry.get("outcome") == "revise-major"
    )

    # ── First-pass completion: plan→done with no revise/pause/abort ──
    pause_or_abort = any(
        entry.get("outcome") in ("pause", "abort")
        for entry in reflection_log
    )
    first_pass_completion = (
        final_status == "done"
        and revise_minor_count == 0
        and revise_major_count == 0
        and not pause_or_abort
    )

    # ── Failure taxonomy ──
    failure_taxonomy = _classify_failures(
        execution_result=execution_result,
        audit_rows=audit_rows,
        tasks=tasks,
    )

    # ── Context-budget rollups ──
    budget_rows = [r for r in audit_rows if r.get("actor") == "context_budget"]
    context_budget_overcap_count = sum(
        1 for r in budget_rows if r.get("refusal_fired") is True
    )
    context_budget_inband_count = sum(
        1 for r in budget_rows
        if r.get("refusal_fired") is not True
        and (r.get("soft_warn_fired") is True
             or r.get("compression_fired") is True)
    )

    # ── Compaction skip counts by skip_reason ──
    compaction_skip_counts: dict[str, int] = {}
    for r in audit_rows:
        if (r.get("actor") == "compression"
                and r.get("event") == "compaction_skipped"):
            reason = r.get("skip_reason") or "unknown"
            compaction_skip_counts[reason] = (
                compaction_skip_counts.get(reason, 0) + 1
            )
    compaction_problem_skip_counts = {
        k: v for k, v in compaction_skip_counts.items()
        if k in _COMPACTION_PROBLEM_SKIP_REASONS
    }

    # ── Inbox utility ──
    inbox_rows = [r for r in audit_rows if r.get("actor") == "inbox"]
    propose_emit_count = sum(
        1 for r in inbox_rows if r.get("event") == "propose_emit"
    )
    propose_accept_count = sum(
        1 for r in inbox_rows if r.get("event") == "propose_accept"
    )
    propose_accept_ratio = (
        propose_accept_count / propose_emit_count
        if propose_emit_count > 0
        else None
    )
    durable_note_read_count = sum(
        1 for r in inbox_rows if r.get("event") == "read"
    )

    # ── Token + cost totals (plan-local usage log) ──
    if usage_rows:
        # Each row carries running totals; the last row holds the
        # cumulative figures.
        last = usage_rows[-1]
        tokens_total = int(last.get("tokens_total") or 0)
        cost_usd = float(last.get("cost_total") or 0.0)
    else:
        tokens_total = 0
        cost_usd = 0.0

    return MetricSnapshot(
        execution_success_rate=execution_success_rate,
        first_pass_qc_accept_rate=first_pass_qc_accept_rate,
        qc_reject_retry_count=qc_reject_retry_count,
        qc_authored_fix_count=qc_authored_fix_count,
        qc_authored_fix_unverified_count=qc_authored_fix_unverified_count,
        divergence_note_count=divergence_note_count,
        revise_minor_count=revise_minor_count,
        revise_major_count=revise_major_count,
        first_pass_completion=first_pass_completion,
        failure_taxonomy=failure_taxonomy,
        context_budget_overcap_count=context_budget_overcap_count,
        context_budget_inband_count=context_budget_inband_count,
        compaction_skip_counts=compaction_skip_counts,
        compaction_problem_skip_counts=compaction_problem_skip_counts,
        propose_emit_count=propose_emit_count,
        propose_accept_count=propose_accept_count,
        propose_accept_ratio=propose_accept_ratio,
        durable_note_read_count=durable_note_read_count,
        tokens_total=tokens_total,
        cost_usd=cost_usd,
        wall_clock_seconds=wall_clock_seconds,
    )


def _qc_transitions_by_task(
    tasks: list[Any] | None,
) -> dict[str, list[Any]]:
    """Group QC-actor :class:`StateTransition` records by task id.

    Tolerant of:

      - ``tasks is None`` (no store access) → empty dict.
      - Tasks with no ``transitions`` attr (unusual but possible in
        synthetic test fixtures) → skipped silently.
      - Transitions whose ``actor`` is None or non-string → skipped.

    Returns a dict ``{task_id: [transitions in append order]}``
    containing ONLY transitions with ``actor == "qc"``. Tasks with
    zero QC transitions don't appear in the dict — matches the old
    audit-row code's behavior of computing
    ``first_pass_qc_accept_rate`` over the set of tasks that
    actually got QC verdicts."""
    out: dict[str, list[Any]] = {}
    if not tasks:
        return out
    for task in tasks:
        transitions = getattr(task, "transitions", None)
        if not transitions:
            continue
        task_id = getattr(task, "id", None)
        if not task_id:
            continue
        qc_transitions = [
            t for t in transitions
            if getattr(t, "actor", None) == "qc"
        ]
        if qc_transitions:
            out[task_id] = qc_transitions
    return out


def _qc_transition_passed(transition: Any) -> bool:
    """Return True iff a QC :class:`StateTransition` records a
    passing verdict. The engine writes ``verifier_result='qc_passed'``
    on the COMPLETED transition and ``verifier_result='qc_failed'``
    (or other non-pass values like ``'environmental_gap'``) on
    rejection-class transitions. ``qc_passed`` is the only string
    that counts as a pass; everything else is treated as a
    rejection so environmental + escalation-path rejections both
    drive ``qc_reject_retry_count`` correctly."""
    return getattr(transition, "verifier_result", None) == "qc_passed"


def _classify_failures(
    *,
    execution_result: dict,
    audit_rows: list[dict],
    tasks: list[Any] | None = None,
) -> dict[str, int]:
    """Classify execution failures into a fixed taxonomy. Read from
    the ExecutionResult error string, audit-row patterns, and
    per-task transitions:

      - ``runtime`` — generic Python exception during execution
      - ``parse`` — reflection-response parse failures
      - ``budget`` — context-budget refusals (audit.jsonl)
      - ``qc`` — terminal QC-rejection states (Task.transitions)

    Counts are independent (a single execution failure can match
    multiple buckets if its symptoms span them).

    Nemo Medium 1 (2026-05-19): the terminal-QC-rejection count
    now reads from ``Task.transitions`` for the same reason
    :func:`extract_metric_snapshot`'s QC block does — engine never
    wrote ``actor='qc'`` rows to ``audit.jsonl``.
    """
    taxonomy = {"runtime": 0, "parse": 0, "budget": 0, "qc": 0}

    # Budget refusals from telemetry
    budget_refusals = sum(
        1 for r in audit_rows
        if r.get("actor") == "context_budget"
        and r.get("refusal_fired") is True
    )
    taxonomy["budget"] = budget_refusals

    # Terminal QC rejections — final QC transition was a non-pass.
    qc_transitions_by_task = _qc_transitions_by_task(tasks)
    terminal_qc_rejections = 0
    for _, transitions in qc_transitions_by_task.items():
        if transitions and not _qc_transition_passed(transitions[-1]):
            terminal_qc_rejections += 1
    taxonomy["qc"] = terminal_qc_rejections

    # Runtime + parse failures from the ExecutionResult error
    error = (execution_result.get("error") or "").lower()
    if error:
        if "parse" in error or "reflection" in error:
            taxonomy["parse"] += 1
        else:
            taxonomy["runtime"] += 1

    return taxonomy


# ─── Public driver: run_experiment ───────────────────────────────────────


def _make_experiment_id() -> str:
    """Return a short, lex-sortable experiment id."""
    import uuid
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"exp-{stamp}-{uuid.uuid4().hex[:6]}"


def _arm_value_for(config: ABConfig, arm: str) -> Any:
    """Pick the right arm value for an arm letter."""
    if arm == "a":
        return config.arm_a_value
    if arm == "b":
        return config.arm_b_value
    raise ABHarnessInvalidConfigError(
        f"unknown arm {arm!r}; expected 'a' or 'b'"
    )


def _validate_config(config: ABConfig) -> None:
    """Raise :class:`ABHarnessInvalidConfigError` if the config carries
    out-of-band enum values, impossible numerics, or arm values whose
    type doesn't match the varied dimension's expected schema."""
    if config.varied_dimension not in VARIED_DIMENSION_LITERALS:
        raise ABHarnessInvalidConfigError(
            f"varied_dimension={config.varied_dimension!r} not in "
            f"{VARIED_DIMENSION_LITERALS}"
        )
    if config.mode not in MODE_LITERALS:
        raise ABHarnessInvalidConfigError(
            f"mode={config.mode!r} not in {MODE_LITERALS}"
        )
    if config.replicates < 1:
        raise ABHarnessInvalidConfigError(
            f"replicates={config.replicates}; must be ≥ 1"
        )
    # Nemo Blocker 2 close-out (2026-05-19): arm values for boolean
    # dimensions must be ACTUAL bools. Without this guard, a loosely
    # typed config (a JSON/YAML/CLI string ``"False"``, or an int like
    # ``0``) gets coerced by ``bool(...)`` to True and silently runs
    # both arms with compression enabled while the report still claims
    # arm B was ``"False"``. ``type(x) is bool`` rather than
    # ``isinstance(x, bool)`` deliberately — Python's ``True == 1`` and
    # ``False == 0`` mean ``isinstance(0, bool)`` is False but the bool
    # constructor still happily coerces 0/1 to bool.
    if config.varied_dimension == "compression":
        for label, value in (
            ("arm_a_value", config.arm_a_value),
            ("arm_b_value", config.arm_b_value),
        ):
            if type(value) is not bool:
                raise ABHarnessInvalidConfigError(
                    f"{label}={value!r} (type={type(value).__name__}); "
                    f"varied_dimension='compression' requires a strict "
                    f"bool (True or False). Strings like 'False' and "
                    f"ints like 0/1 are rejected to prevent silent "
                    f"coercion of arm values."
                )


def _apply_arm_override(
    *,
    base_config: dict,
    varied_dimension: str,
    arm_value: Any,
) -> dict:
    """Deep-copy the base project config and apply the arm override.
    Returns a fresh dict so per-arm mutations don't leak."""
    import copy
    effective = copy.deepcopy(base_config)
    if varied_dimension == "compression":
        # Nemo Blocker 2 (2026-05-19): direct assign, NOT ``bool(...)``.
        # ``_validate_config`` has already enforced ``type(...) is bool``
        # for compression arm values, so a non-bool reaching this point
        # is a programmer error and should fault on the dict op, not be
        # silently coerced. The earlier ``bool(arm_value)`` was the bug:
        # ``bool("False") is True``, ``bool(0) is False``, etc.
        effective["compression_enabled"] = arm_value
    else:
        # Future dimensions land here. For now, write the dimension
        # name as a kwarg so the caller's factory can read it back.
        effective[varied_dimension] = arm_value
    return effective


def run_experiment(
    config: ABConfig,
    *,
    output_root: Path,
    allow_paid_live: bool = False,
    replicate_factory: Any = None,
) -> ExperimentReport:
    """Run one A/B experiment end-to-end.

    ``replicate_factory(arm, replicate_index, effective_config)`` is
    required and must return a tuple of
    ``(project, runners, kickoff_callable, plan_record_or_None,
      reflect_runner_or_None, usage_log_path_or_None)``. The factory
    is responsible for ``vault.init_project`` + ``vault.init_run``
    per replicate (fresh ``run_id`` is the isolation boundary —
    Nemo Round-1 implementation seam). The SAME factory shape works
    for both modes; what differs is whether the runners are
    hand-crafted (stub) or real LLM-backed (live). The harness is
    mode-agnostic at this seam.

    Stub mode (``config.mode == "stub"``):
      No cost gate. Deterministic. Default for tests + CI.

    Live mode (``config.mode == "live"``):
      Requires ``allow_paid_live=True`` or raises
      :class:`ABHarnessUnconfirmedLiveCostError`. The factory's
      runner construction is where determinism controls
      (``temperature=0``, ``seed`` propagation) must be applied —
      the harness cannot enforce them through this interface.

    Failure handling: per-replicate exceptions are caught, logged
    via :func:`emit_replicate_failed`, and DO NOT abort the
    experiment. Other replicates continue. Aggregation handles
    partial success via ``n_successful`` vs ``n_attempted``.

    Returns a frozen :class:`ExperimentReport` after materializing:

      - ``<output_root>/<experiment_id>/config.json``
      - ``<output_root>/<experiment_id>/audit.jsonl``
      - ``<output_root>/<experiment_id>/arm_a/<NNN>/manifest.json``
      - ``<output_root>/<experiment_id>/arm_a/<NNN>/metrics.json``
      - ``<output_root>/<experiment_id>/arm_a/aggregate.json``
      - (same for arm_b)
      - ``<output_root>/<experiment_id>/report.json``
      - ``<output_root>/<experiment_id>/report.md``
    """
    _validate_config(config)

    if config.mode == "live" and not allow_paid_live:
        raise ABHarnessUnconfirmedLiveCostError(
            "mode='live' requires allow_paid_live=True. Call "
            "estimate_experiment(config) first to see the cost "
            "estimate, then re-invoke run_experiment with the "
            "explicit opt-in flag."
        )

    if replicate_factory is None:
        raise ABHarnessInvalidConfigError(
            "run_experiment requires replicate_factory= callback "
            "(mode-agnostic; same shape for stub + live)"
        )

    # ── Pre-flight ────────────────────────────────────────────────
    experiment_id = _make_experiment_id()
    experiment_dir = output_root / experiment_id
    experiment_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    experiment_audit = experiment_dir / "audit.jsonl"

    started_at = _utcnow_iso()
    started_wall = _wall_clock_now()
    cost_estimate = estimate_experiment(config)

    # config_hash is over the full ABConfig as-rendered.
    config_render = {
        "base_project_config": config.base_project_config,
        "varied_dimension": config.varied_dimension,
        "arm_a_value": config.arm_a_value,
        "arm_b_value": config.arm_b_value,
        "replicates": config.replicates,
        "mode": config.mode,
        "order_seed": config.order_seed,
    }
    config_hash = compute_config_hash(config_render)

    # Compute + persist replicate order.
    replicate_order = _replicate_order(
        replicates=config.replicates, order_seed=config.order_seed,
    )

    # Persist config.json with the actual order stamped in (Nemo
    # Round-1 #5 — order seed lands in config.json for reproducibility).
    _write_artifact_0600(
        experiment_dir / "config.json",
        json.dumps(
            {
                **config_render,
                "config_hash": config_hash,
                "experiment_id": experiment_id,
                "replicate_order": replicate_order,
            },
            indent=2,
            sort_keys=True,
            default=str,
            ensure_ascii=True,
        ),
    )

    emit_experiment_started(
        audit_path=experiment_audit,
        experiment_id=experiment_id,
        varied_dimension=config.varied_dimension,
        config_hash=config_hash,
        started_at=started_at,
    )

    # ── Replicate loop ─────────────────────────────────────────────
    arm_a_snapshots: list[MetricSnapshot] = []
    arm_b_snapshots: list[MetricSnapshot] = []
    arm_a_manifest_paths: list[str] = []
    arm_b_manifest_paths: list[str] = []
    n_attempted_a = 0
    n_attempted_b = 0

    for arm, replicate_index in replicate_order:
        if arm == "a":
            n_attempted_a += 1
        else:
            n_attempted_b += 1

        arm_value = _arm_value_for(config, arm)
        effective_config = _apply_arm_override(
            base_config=config.base_project_config,
            varied_dimension=config.varied_dimension,
            arm_value=arm_value,
        )
        replicate_dir = (
            experiment_dir / f"arm_{arm}" / f"{replicate_index:03d}"
        )
        replicate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        replicate_started_at = _utcnow_iso()
        replicate_started_wall = _wall_clock_now()

        # Snapshot to None up front so the failure path has values
        manifest: ReplicateManifest | None = None
        metric_snap: MetricSnapshot | None = None
        error_str: str | None = None
        project = None
        plan = None
        execution_result_dict: dict = {}
        run_dir_str = ""
        engine_audit_str = ""
        usage_log_path = None

        try:
            from modulatio import project_execution as _project_execution
            from modulatio import vault as _vault

            # Caller's factory provides everything the replicate needs.
            (
                project,
                runners,
                kickoff_callable,
                plan,
                reflect_runner,
                usage_log_path,
            ) = replicate_factory(arm, replicate_index, effective_config)

            run_dir = _vault.run_dir(project.code, project.run_id)
            run_dir_str = str(run_dir)
            engine_audit_str = str(run_dir / "audit.jsonl")

            emit_replicate_started(
                audit_path=experiment_audit,
                experiment_id=experiment_id,
                varied_dimension=config.varied_dimension,
                config_hash=config_hash,
                arm=arm,
                replicate_index=replicate_index,
                effective_arm_value=arm_value,
                project_code=project.code,
                run_id=project.run_id,
                run_dir=run_dir_str,
                audit_path_engine=engine_audit_str,
                started_at=replicate_started_at,
            )

            if plan is not None:
                exec_result = _project_execution.start_execution(
                    plan.id, project,
                    runners=runners,
                    reflect_runner=reflect_runner,
                    kickoff_callable=kickoff_callable,
                )
            else:
                # No plan provided — caller did the dispatch its own way
                exec_result = None

            execution_result_dict = (
                exec_result.__dict__.copy() if exec_result else {}
            )

            replicate_finished_at = _utcnow_iso()
            wall_clock = _wall_clock_now() - replicate_started_wall

            # Nemo Medium 1 (2026-05-19): pull the replicate's Task
            # records so the snapshot can read QC verdicts from
            # ``Task.transitions`` (the engine's actual persistence
            # surface) rather than ``audit.jsonl`` (where they never
            # land). Best-effort: store lookup failures don't break
            # the replicate — QC metrics just degrade to None / 0.
            replicate_tasks: list[Any] = []
            try:
                from modulatio import store as _store
                replicate_tasks = _store.list_tasks(
                    project.code, run_id=project.run_id,
                )
            except Exception:  # noqa: BLE001 — best-effort QC source
                replicate_tasks = []

            # Build the metric snapshot from the engine's audit log
            metric_snap = extract_metric_snapshot(
                audit_path=run_dir / "audit.jsonl",
                usage_log_path=usage_log_path,
                reflection_log=(
                    exec_result.reflection_log if exec_result else None
                ),
                execution_result=execution_result_dict,
                wall_clock_seconds=wall_clock,
                tasks=replicate_tasks,
            )

            # Build manifest
            manifest = ReplicateManifest(
                arm=arm,
                replicate_index=replicate_index,
                project_code=project.code,
                run_id=project.run_id,
                run_dir=run_dir_str,
                audit_path=engine_audit_str,
                plan_id=plan.id if plan else None,
                plan_final_status=(
                    exec_result.final_status if exec_result else None
                ),
                usage_log_path=str(usage_log_path) if usage_log_path else None,
                execution_result=execution_result_dict,
                effective_config=effective_config,
                config_hash=compute_config_hash(effective_config),
                status="completed",
                started_at=replicate_started_at,
                finished_at=replicate_finished_at,
                duration_seconds=wall_clock,
                error=None,
            )

            # Persist manifest + metrics
            _write_artifact_0600(
                replicate_dir / "manifest.json",
                json.dumps(
                    manifest.__dict__, indent=2, sort_keys=True,
                    default=str, ensure_ascii=True,
                ),
            )
            _write_artifact_0600(
                replicate_dir / "metrics.json",
                json.dumps(
                    metric_snap.__dict__, indent=2, sort_keys=True,
                    default=str, ensure_ascii=True,
                ),
            )

            emit_replicate_completed(
                audit_path=experiment_audit,
                experiment_id=experiment_id,
                varied_dimension=config.varied_dimension,
                config_hash=config_hash,
                arm=arm,
                replicate_index=replicate_index,
                effective_arm_value=arm_value,
                project_code=project.code,
                run_id=project.run_id,
                run_dir=run_dir_str,
                audit_path_engine=engine_audit_str,
                started_at=replicate_started_at,
                finished_at=replicate_finished_at,
                duration_seconds=wall_clock,
            )

            if arm == "a":
                arm_a_snapshots.append(metric_snap)
                arm_a_manifest_paths.append(
                    str(replicate_dir / "manifest.json")
                )
            else:
                arm_b_snapshots.append(metric_snap)
                arm_b_manifest_paths.append(
                    str(replicate_dir / "manifest.json")
                )
        except Exception as exc:  # noqa: BLE001 — per-replicate isolation
            replicate_finished_at = _utcnow_iso()
            wall_clock = _wall_clock_now() - replicate_started_wall
            error_str = f"{type(exc).__name__}: {exc}"
            _logger.warning(
                "replicate %s%d failed: %s",
                arm, replicate_index, error_str,
            )

            # Best-effort failure manifest
            fail_manifest = ReplicateManifest(
                arm=arm,
                replicate_index=replicate_index,
                project_code=project.code if project else "?",
                run_id=project.run_id if project else "?",
                run_dir=run_dir_str,
                audit_path=engine_audit_str,
                plan_id=plan.id if plan else None,
                plan_final_status=None,
                usage_log_path=str(usage_log_path) if usage_log_path else None,
                execution_result=execution_result_dict,
                effective_config=effective_config,
                config_hash=compute_config_hash(effective_config),
                status="failed",
                started_at=replicate_started_at,
                finished_at=replicate_finished_at,
                duration_seconds=wall_clock,
                error=error_str,
            )
            try:
                _write_artifact_0600(
                    replicate_dir / "manifest.json",
                    json.dumps(
                        fail_manifest.__dict__, indent=2, sort_keys=True,
                        default=str, ensure_ascii=True,
                    ),
                )
            except Exception:
                pass

            emit_replicate_failed(
                audit_path=experiment_audit,
                experiment_id=experiment_id,
                varied_dimension=config.varied_dimension,
                config_hash=config_hash,
                arm=arm,
                replicate_index=replicate_index,
                effective_arm_value=arm_value,
                project_code=project.code if project else None,
                run_id=project.run_id if project else None,
                run_dir=run_dir_str or None,
                audit_path_engine=engine_audit_str or None,
                started_at=replicate_started_at,
                finished_at=replicate_finished_at,
                duration_seconds=wall_clock,
                error=error_str,
            )

    # ── Aggregation ────────────────────────────────────────────────
    arm_a_agg = aggregate_arm(
        arm="a",
        snapshots=arm_a_snapshots,
        per_replicate_paths=arm_a_manifest_paths,
        n_attempted=n_attempted_a,
    )
    arm_b_agg = aggregate_arm(
        arm="b",
        snapshots=arm_b_snapshots,
        per_replicate_paths=arm_b_manifest_paths,
        n_attempted=n_attempted_b,
    )

    # Persist arm aggregates
    _write_artifact_0600(
        experiment_dir / "arm_a" / "aggregate.json",
        json.dumps(
            arm_a_agg.__dict__, indent=2, sort_keys=True,
            default=str, ensure_ascii=True,
        ),
    )
    _write_artifact_0600(
        experiment_dir / "arm_b" / "aggregate.json",
        json.dumps(
            arm_b_agg.__dict__, indent=2, sort_keys=True,
            default=str, ensure_ascii=True,
        ),
    )

    # ── Deltas + report ────────────────────────────────────────────
    deltas = compute_deltas(arm_a=arm_a_agg, arm_b=arm_b_agg)
    finished_at = _utcnow_iso()
    duration_seconds = _wall_clock_now() - started_wall
    total_cost = sum(
        s.cost_usd for s in arm_a_snapshots + arm_b_snapshots
    )

    report = ExperimentReport(
        experiment_id=experiment_id,
        config=config,
        config_hash=config_hash,
        arm_a=arm_a_agg,
        arm_b=arm_b_agg,
        deltas=deltas,
        total_cost_usd=total_cost,
        cost_estimate_pre_flight=cost_estimate,
        started_at=started_at,
        finished_at=finished_at,
        replicate_order=replicate_order,
        interpretation_note=INTERPRETATION_NOTE,
    )

    # Persist report (JSON + markdown)
    report_serializable = {
        "experiment_id": report.experiment_id,
        "config_hash": report.config_hash,
        "config": config_render,
        "arm_a": arm_a_agg.__dict__,
        "arm_b": arm_b_agg.__dict__,
        "deltas": {k: v.__dict__ for k, v in deltas.items()},
        "total_cost_usd": report.total_cost_usd,
        "cost_estimate_pre_flight": cost_estimate.__dict__,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "replicate_order": report.replicate_order,
        "interpretation_note": report.interpretation_note,
    }
    _write_artifact_0600(
        experiment_dir / "report.json",
        json.dumps(
            report_serializable, indent=2, sort_keys=True,
            default=str, ensure_ascii=True,
        ),
    )
    _write_artifact_0600(
        experiment_dir / "report.md",
        render_report_markdown(report),
    )

    emit_experiment_completed(
        audit_path=experiment_audit,
        experiment_id=experiment_id,
        varied_dimension=config.varied_dimension,
        config_hash=config_hash,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        total_cost_usd=total_cost,
    )

    return report


def _wall_clock_now() -> float:
    """Monotonic seconds since some arbitrary epoch — for measuring
    wall-clock duration without DST / clock-skew artifacts."""
    import time
    return time.monotonic()


__all__ = [
    "AB_HARNESS_EVENT_LITERALS",
    "ABConfig",
    "ABHarnessError",
    "ABHarnessInvalidConfigError",
    "ABHarnessUnconfirmedLiveCostError",
    "ArmAggregate",
    "CostEstimate",
    "ExperimentReport",
    "INTERPRETATION_NOTE",
    "MODE_LITERALS",
    "MetricDelta",
    "MetricSnapshot",
    "ReplicateManifest",
    "VARIED_DIMENSION_LITERALS",
    "aggregate_arm",
    "compute_config_hash",
    "compute_deltas",
    "emit_experiment_completed",
    "emit_experiment_started",
    "emit_replicate_completed",
    "emit_replicate_failed",
    "emit_replicate_started",
    "estimate_experiment",
    "extract_metric_snapshot",
    "render_report_markdown",
    "run_experiment",
]
