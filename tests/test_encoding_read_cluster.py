"""0.9.0 re-sweep regression: structured/config readers must survive a non-UTF-8
or genuinely-corrupt file — degrade (empty/rebuild), never crash a boot path, a
rebuildable cache, or a per-request hot path.

The write-sweep pinned every writer to UTF-8; this locks the READ side. Under a
C/POSIX locale a valid UTF-8 file used to fail to decode at all; a corrupt byte
used to raise UnicodeDecodeError straight out of an except that only caught
JSONDecodeError. The verified findings (5 were elevated to HIGH before being
re-rated MEDIUM) all share this root.
"""
from __future__ import annotations

import json

# A byte sequence that is NOT valid UTF-8 (lone 0xFF / 0xFE).
NON_UTF8 = b"\xff\xfe\x00garbage\x81\x82"


def test_model_presets_load_survives_non_utf8(tmp_path, monkeypatch):
    from modulatio import model_presets

    f = tmp_path / "model_presets.json"
    f.write_bytes(NON_UTF8)
    monkeypatch.setattr(model_presets, "PRESETS_FILE", f)
    # must not raise UnicodeDecodeError — degrades to empty
    assert model_presets.load_presets() == {}


def test_config_defaults_load_survives_non_utf8(tmp_path, monkeypatch):
    from modulatio import config

    f = tmp_path / "defaults.json"
    f.write_bytes(NON_UTF8)
    monkeypatch.setattr(config, "DEFAULTS_FILE", f)
    monkeypatch.setattr(config, "_cached_defaults", None)
    assert config._load_defaults() == {}


def test_qc_history_load_meta_survives_non_utf8(tmp_path, monkeypatch):
    from modulatio import qc_history

    meta = tmp_path / "meta.json"
    meta.write_bytes(NON_UTF8)
    monkeypatch.setattr(qc_history, "_meta_path", lambda *a, **k: meta)
    # rebuildable cache: a corrupt meta returns {} (triggers a clean rebuild)
    assert qc_history._load_meta("PROJ", "document") == {}


def test_qc_history_load_meta_reads_valid_utf8_under_any_locale(tmp_path, monkeypatch):
    """The locale bug: a VALID utf-8 meta (non-ASCII content) must read back."""
    from modulatio import qc_history

    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"señor": "café ☕"}), encoding="utf-8")
    monkeypatch.setattr(qc_history, "_meta_path", lambda *a, **k: meta)
    assert qc_history._load_meta("PROJ", "document") == {"señor": "café ☕"}
