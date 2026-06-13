"""LOW-severity audit regressions for ``ab_harness`` (findings #43, #44).

Uniquely named to avoid colliding with the main module test file while
sibling agents edit concurrently.

  #43 _write_artifact_0600 must not double-close the fd on a swallowed
      write failure, and the OSError fallback must still write the file.
  #44 total_cost_usd must include money spent by replicates that crashed
      after issuing LLM calls (no snapshot produced for those).
"""

from __future__ import annotations

import json
import os
import stat

from modulatio import ab_harness
from modulatio.ab_harness import _usage_cost_total, _write_artifact_0600


# ── #43: _write_artifact_0600 ────────────────────────────────────────


def test_write_artifact_0600_writes_with_restrictive_mode(tmp_path):
    target = tmp_path / "sub" / "config.json"
    _write_artifact_0600(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # Parent dir created 0o700.
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_write_artifact_0600_no_double_close_on_write_failure(
    tmp_path, monkeypatch
):
    """If the underlying write raises, the fdopen context manager owns
    the fd and closes it exactly once. A second os.close on a reused
    descriptor is the regressed double-close; assert os.close is called
    at most once on the experiment's fd."""
    target = tmp_path / "config.json"

    real_open = os.open
    captured_fd: dict[str, int] = {}

    def tracking_open(path, flags, mode=0o777, *a, **k):
        fd = real_open(path, flags, mode, *a, **k)
        # Only track the artifact's own descriptor.
        if str(path) == str(target):
            captured_fd["fd"] = fd
        return fd

    real_close = os.close
    close_calls: list[int] = []

    def tracking_close(fd):
        close_calls.append(fd)
        return real_close(fd)

    class _BoomWriter:
        def __init__(self, fh):
            self._fh = fh

        def write(self, _text):
            raise RuntimeError("boom during write")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            # Mirror fdopen's real behaviour: close the wrapped fd on
            # exit, including the exception path.
            self._fh.close()
            return False

    real_fdopen = os.fdopen

    def boom_fdopen(fd, *a, **k):
        return _BoomWriter(real_fdopen(fd, *a, **k))

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", tracking_close)
    monkeypatch.setattr(os, "fdopen", boom_fdopen)

    # A non-OSError escapes (it's a real data/logic error, not a
    # platform issue) — but the fd must NOT be double-closed.
    try:
        _write_artifact_0600(target, "payload")
    except RuntimeError:
        pass

    fd = captured_fd.get("fd")
    assert fd is not None
    # The artifact fd is closed exactly once (by fdopen's __exit__),
    # never a second time by a manual os.close on a reused descriptor.
    assert close_calls.count(fd) <= 1


def test_write_artifact_0600_oserror_fallback(tmp_path, monkeypatch):
    """When os.open raises OSError (read-only mount / odd platform),
    fall back to write_text so the experiment still completes."""
    target = tmp_path / "config.json"

    def raising_open(*a, **k):
        raise OSError("read-only mount")

    monkeypatch.setattr(os, "open", raising_open)
    _write_artifact_0600(target, "fallback-content")
    assert target.read_text(encoding="utf-8") == "fallback-content"


# ── #44: crashed-replicate cost accounting ───────────────────────────


def _write_usage_log(path, *cost_totals):
    with path.open("w", encoding="utf-8") as fh:
        for i, c in enumerate(cost_totals):
            fh.write(
                json.dumps({"tokens_total": (i + 1) * 100, "cost_total": c})
                + "\n"
            )


def test_usage_cost_total_reads_last_running_total(tmp_path):
    log = tmp_path / "plan.usage.jsonl"
    _write_usage_log(log, 0.01, 0.05, 0.12)
    # Mirrors extract_metric_snapshot: the LAST row holds the cumulative
    # cost.
    assert _usage_cost_total(log) == 0.12


def test_usage_cost_total_none_path_is_zero():
    assert _usage_cost_total(None) == 0.0


def test_usage_cost_total_missing_file_is_zero(tmp_path):
    assert _usage_cost_total(tmp_path / "does_not_exist.jsonl") == 0.0


def test_usage_cost_total_malformed_cost_is_zero(tmp_path):
    log = tmp_path / "plan.usage.jsonl"
    log.write_text(
        json.dumps({"cost_total": "not-a-number"}) + "\n", encoding="utf-8"
    )
    assert _usage_cost_total(log) == 0.0


def test_usage_cost_total_matches_snapshot_cost(tmp_path):
    """The crash-path cost extractor must agree with the value
    extract_metric_snapshot would compute for the same usage log, so a
    crashed replicate contributes the same cost a successful one
    would."""
    log = tmp_path / "plan.usage.jsonl"
    _write_usage_log(log, 0.02, 0.07)
    snap = ab_harness.extract_metric_snapshot(
        audit_path=tmp_path / "audit.jsonl",  # absent → empty rows
        usage_log_path=log,
        reflection_log=None,
        execution_result={},
        wall_clock_seconds=1.0,
    )
    assert _usage_cost_total(log) == snap.cost_usd == 0.07
