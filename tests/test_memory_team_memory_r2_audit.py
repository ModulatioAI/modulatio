"""Round-2 full-debug audit regressions for team_memory (3 LOW findings).

Each test fails against the pre-fix code and passes after:

  1. error-path: _parse() catches OSError but NOT UnicodeDecodeError, so a
     binary / non-UTF-8 .md bricks list_entries()/recall() for every valid
     entry. UnicodeDecodeError is a ValueError subclass, not an OSError.
  2. correctness: the embedding model was captured at import (_EMBED_MODEL),
     so a wizard model-swap mid-process never changed the LanceDB metadata
     fingerprint → no rebuild → stale vectors. Resolve per-call instead.
  3. correctness: the recency fallback labelled unscored entries sim 1.00,
     which bypasses min_similarity and misrepresents non-semantic precedent
     to the producer. Use a None sentinel rendered as "recency".
"""

from __future__ import annotations

import pytest

from modulatio import config, vault
from modulatio.memory import team_memory


PROJECT_CODE = "R2"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({
        "vault_root": str(tmp_path / "vault"),
        "cache_root": str(tmp_path / "cache"),
    })
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


# === Finding 1: binary / non-UTF-8 entry must not brick the listing ===

def test_parse_skips_non_utf8_entry_instead_of_crashing():
    # A valid entry plus a corrupt (binary / non-UTF-8) .md in the same dir.
    good = team_memory.write(
        writer_id="qc-1", writer_tier="qc",
        body="valid precedent body",
        project_code=PROJECT_CODE, artifact_kind="report",
    )
    team_dir = team_memory._team_dir(PROJECT_CODE)
    (team_dir / "20260101__corrupt.md").write_bytes(b"\xff\xfe binary garbage \x00")

    # Pre-fix: UnicodeDecodeError propagates out of _parse → list_entries dies.
    entries = team_memory.list_entries(PROJECT_CODE)
    ids = {e.entry_id for e in entries}
    assert good.entry_id in ids  # valid entry still surfaced
    # The corrupt file is quarantined (skipped), not crashing the listing.


def test_parse_returns_none_on_undecodable_file():
    team_dir = team_memory._team_dir(PROJECT_CODE)
    team_dir.mkdir(parents=True, exist_ok=True)
    bad = team_dir / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00\x80\x81")
    assert team_memory._parse(bad) is None


# === Finding 2: embedding model resolved per-call, not captured at import ===

def test_embed_model_resolves_current_config(monkeypatch):
    # The module must NOT serve a value frozen at import. Swap the config
    # value at runtime and confirm _embed_model() reflects it.
    monkeypatch.setattr(config, "get_embedding_model", lambda: "swapped-model-x")
    assert team_memory._embed_model() == "swapped-model-x"


def test_config_hash_tracks_live_model_swap(monkeypatch):
    rec = team_memory.write(
        writer_id="qc-1", writer_tier="qc", body="b",
        project_code=PROJECT_CODE, artifact_kind="report",
    )
    records = team_memory.list_entries(PROJECT_CODE)
    assert records  # sanity

    monkeypatch.setattr(config, "get_embedding_model", lambda: "model-a")
    hash_a = team_memory._config_hash(records)
    monkeypatch.setattr(config, "get_embedding_model", lambda: "model-b")
    hash_b = team_memory._config_hash(records)
    # Pre-fix the hash was pinned to the import-time _EMBED_MODEL and never
    # changed; the rebuild gate would therefore miss a model swap.
    assert hash_a != hash_b
    _ = rec


# === Finding 3: recency fallback must not claim sim 1.00 ===

def _seed_one():
    team_memory.write(
        writer_id="qc-1", writer_tier="qc",
        body="precedent body text",
        project_code=PROJECT_CODE, artifact_kind="report",
    )


def test_recency_fallback_score_is_none_not_one():
    _seed_one()
    # No embedder / no task_description → recency fallback path.
    hits = team_memory.recall(project_code=PROJECT_CODE, artifact_kind="report")
    assert hits
    rec, sim = hits[0]
    assert sim is None  # unscored sentinel, not a fabricated 1.0


def test_render_for_prompt_labels_recency_not_sim_one():
    _seed_one()
    hits = team_memory.recall(project_code=PROJECT_CODE, artifact_kind="report")
    rendered = team_memory.render_for_prompt(hits)
    assert "| recency |" in rendered
    assert "sim 1.00" not in rendered


def test_render_for_prompt_still_shows_numeric_sim_for_scored_hits():
    # A genuinely-scored hit (float) must still render as "sim X.XX".
    team_memory.write(
        writer_id="qc-1", writer_tier="qc",
        body="scored precedent body",
        project_code=PROJECT_CODE, artifact_kind="report",
    )
    entry = team_memory.list_entries(PROJECT_CODE)[0]
    rendered = team_memory.render_for_prompt([(entry, 0.83)])
    assert "sim 0.83" in rendered
    # The score field must read "sim ...", never the recency sentinel.
    assert "| recency |" not in rendered
