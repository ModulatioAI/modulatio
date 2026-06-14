"""R2 audit regression: a non-UTF-8 / unreadable skill file must NOT raise
UnicodeDecodeError out of ``_parse_file`` / ``load_with_metadata``.

These files are human-edited artifacts in the shared/project vault. On
dispatch the wave-floor callbacks (orchestration ``_skill_floor_for``) call
``skills.load_with_metadata`` with no try/except, so a single bad byte in one
shared skill .md would otherwise propagate out of ``schedule_wave`` and crash
the entire wave loop (the whole run), not just that one task. A bad file must
degrade to "missing", honoring the documented "missing → empty, not an error"
contract.
"""

from __future__ import annotations



from modulatio import skills


def test_non_utf8_shared_skill_does_not_crash_load(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    bad = shared / "drafter.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    # Latin-1 / truncated-multibyte garbage: invalid UTF-8.
    bad.write_bytes(b"---\nname: drafter\n---\n\xff\xfe binary body \x80\x81")

    # Before the fix this raised UnicodeDecodeError; now it tolerates the bytes.
    entry = skills.load_with_metadata("drafter")
    # The replacement-decoded body still parses; the skill is usable.
    assert entry.name == "drafter"
    assert isinstance(entry.prompt_template, str)


def test_non_utf8_skill_parse_file_direct(tmp_path):
    bad = tmp_path / "x.md"
    bad.write_bytes(b"\xff\xfe not valid utf-8 \x80")
    # Must not raise.
    sk = skills._parse_file(bad)
    assert isinstance(sk.prompt_template, str)


def test_unreadable_skill_file_degrades_to_empty(tmp_path):
    # A path that exists but errors on read (a directory) degrades to the
    # empty skill rather than crashing.
    d = tmp_path / "adir.md"
    d.mkdir()
    sk = skills._parse_file(d)
    assert sk is skills._EMPTY_SKILL
