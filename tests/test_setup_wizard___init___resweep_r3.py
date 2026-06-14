# SPDX-License-Identifier: Apache-2.0
"""Round-3 re-sweep regression tests for modulatio.setup_wizard.__init__.

Finding 1 [LOW/correctness]: per finding #348 the embedded_llm prefetch step
now runs BEFORE confirm (step 7 of 8). ``embedded_llm_step.prefetch()``
downloads the routing embedder (potentially hundreds of MB) into fastembed's
cache — a durable, reusable on-disk side effect. The abort handler only
inspected model presets and pandoc/clipboard system installs; it did not
account for a freshly-downloaded embedder. On a re-invocation where presets +
system tools are unchanged, an abort AFTER a successful prefetch could claim
"No changes written" even though a model was just written to the cache.

The fix snapshots ``embedded_llm_step.is_cached(active_model)`` at wizard start
(mirroring ``_system_tools_snapshot``) and, on abort, if it flipped to True,
the message owns the (reusable) cache warm instead of claiming nothing changed.

These tests drive the REAL ``run_setup`` to an abort with the cache probe +
presets + system tools mocked, and assert the abort message tells the truth.
They also guard back-compat: a stable cache (no flip) keeps the prior wording.
"""

from __future__ import annotations

from unittest import mock

from modulatio import setup_wizard
from modulatio.setup_wizard import clipboard_step, embedded_llm_step, pandoc_step, steps


def _abort(*_args, **_kwargs):
    raise steps.WizardAborted()


def _run_with(
    *,
    pandoc_seq=(True, True),
    clipboard_seq=(True, True),
    presets_seq=({}, {}),
    embed_cached_seq=(False, False),
    embed_model="BAAI/bge-small-en-v1.5",
):
    """Drive run_setup() to an abort with mocked probes; return muted msgs.

    ``embed_cached_seq`` is [value-at-start, value-at-abort] for
    ``embedded_llm_step.is_cached`` — the embedder cache snapshot.
    """
    muted_calls: list[str] = []
    with (
        mock.patch.object(setup_wizard, "_load_existing_state", return_value={}),
        mock.patch.object(setup_wizard.steps, "run_step_machine", side_effect=_abort),
        mock.patch.object(pandoc_step, "is_installed", side_effect=list(pandoc_seq)),
        mock.patch.object(clipboard_step, "is_installed", side_effect=list(clipboard_seq)),
        mock.patch(
            "modulatio.model_presets.load_presets",
            side_effect=[dict(p) for p in presets_seq],
        ),
        mock.patch.object(
            embedded_llm_step, "is_cached", side_effect=list(embed_cached_seq)
        ),
        mock.patch.object(
            setup_wizard.config, "get_embedding_model", return_value=embed_model
        ),
        mock.patch.object(setup_wizard.theme, "muted", side_effect=muted_calls.append),
        mock.patch.object(setup_wizard.theme, "enter_dark_screen"),
        mock.patch.object(setup_wizard.theme, "exit_dark_screen"),
    ):
        result = setup_wizard.run_setup()
    return result, muted_calls


def test_abort_after_prefetch_does_not_claim_no_changes():
    """Embedder absent at start, cached at abort (prefetched mid-run) → the
    abort message must NOT lie with 'No changes written'."""
    result, muted = _run_with(embed_cached_seq=(False, True))
    assert result is False
    assert len(muted) == 1
    msg = muted[0]
    assert "No changes written" not in msg
    assert "cache" in msg.lower()
    # The active model id is surfaced so the user knows what was downloaded.
    assert "BAAI/bge-small-en-v1.5" in msg


def test_abort_after_prefetch_reports_download_to_reusable_cache():
    """The message frames the side effect as a reusable cache warm (a download
    that survives + is reused), not a destructive write."""
    _result, muted = _run_with(embed_cached_seq=(False, True))
    msg = muted[0]
    assert "downloaded" in msg.lower()
    assert "reusable" in msg.lower()


def test_abort_with_stable_cache_keeps_honest_no_changes():
    """Cache already present at start (and still present) → no flip, so the
    honest 'No changes written' message is preserved (no spurious claim)."""
    result, muted = _run_with(embed_cached_seq=(True, True))
    assert result is False
    assert muted == ["Setup aborted. No changes written."]


def test_abort_with_no_cache_either_time_keeps_honest_no_changes():
    """Embedder never cached during the run → no embedded clause added."""
    result, muted = _run_with(embed_cached_seq=(False, False))
    assert result is False
    assert muted == ["Setup aborted. No changes written."]


def test_abort_reports_prefetch_alongside_presets():
    """When BOTH presets persisted AND the embedder was downloaded, the abort
    message acknowledges both."""
    result, muted = _run_with(
        presets_seq=({}, {"m": {"label": "x"}}),
        embed_cached_seq=(False, True),
    )
    assert result is False
    msg = muted[0]
    assert "No changes written" not in msg
    assert "saved" in msg.lower() or "model changes" in msg.lower()
    assert "cache" in msg.lower()


def test_abort_reports_prefetch_alongside_system_install():
    """Embedder download + a system-tool install are both surfaced; because a
    durable cache write happened, the tail is 'no other settings' not 'no
    configuration'."""
    result, muted = _run_with(
        pandoc_seq=(False, True),
        embed_cached_seq=(False, True),
    )
    assert result is False
    msg = muted[0]
    assert "pandoc" in msg
    assert "cache" in msg.lower()
    assert "no other settings were written" in msg


def test_embedded_model_snapshot_swallows_probe_errors():
    """A cache probe that raises is treated as 'not cached' so the abort path
    can't crash on a flaky is_cached()."""
    with (
        mock.patch.object(
            setup_wizard.config, "get_embedding_model", return_value="x/y"
        ),
        mock.patch.object(
            embedded_llm_step, "is_cached", side_effect=RuntimeError("boom")
        ),
    ):
        model_id, cached = setup_wizard._embedded_model_snapshot()
    assert model_id == "x/y"
    assert cached is False


def test_embedded_model_snapshot_swallows_config_errors():
    """If even resolving the active model id raises, the snapshot returns a
    safe ('', False) rather than crashing the abort path."""
    with mock.patch.object(
        setup_wizard.config,
        "get_embedding_model",
        side_effect=RuntimeError("no config"),
    ):
        model_id, cached = setup_wizard._embedded_model_snapshot()
    assert model_id == ""
    assert cached is False


def test_abort_prefetch_with_unknown_model_id_omits_label_gracefully():
    """If the model id resolves empty (config error path), the clause still
    reads cleanly without a dangling '()' label."""
    # is_cached can't flip to True without a model id in practice, but guard the
    # rendering: an empty model id must not produce '... model () was ...'.
    result, muted = _run_with(embed_cached_seq=(False, True), embed_model="")
    assert result is False
    msg = muted[0]
    assert "()" not in msg
    assert "cache" in msg.lower()
