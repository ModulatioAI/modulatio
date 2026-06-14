"""0.9.0 pre-ship re-sweep (round 3) regressions for src/modulatio/cli.py.

Finding (MEDIUM/edge-case): a directory whose name ends in an image
extension (e.g. ``pics.png``) routes through ``_infer_attachment_kind`` to
``kind='image'``, which never ``read_text()``s — so it slipped past
``build_attachment``'s fail-fast validation and would crash later at
multimodal dispatch, AFTER the project/roster/run folder had already been
created on disk (defeating the orphan-folder fail-fast contract documented
at cli.kickoff). The document branch was protected (``read_text`` on a dir
raises ``IsADirectoryError``); the image branch was not. kickoff() now
rejects any non-regular-file ``--attach`` up front, before any disk write.
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from modulatio import cli, vault


def _invoke_with_attach(attach_path: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    runner = CliRunner()
    return runner.invoke(
        cli.app,
        [
            "kickoff",
            "--code", "DIR",
            "--objective", "improve this picture",
            "--stub",
            "--attach", str(attach_path),
        ],
    )


def test_directory_with_image_ext_attach_fails_fast(tmp_path: Path, monkeypatch):
    """A directory named like an image must fail fast with a clean message,
    NOT build a bogus image Attachment that crashes mid-run."""
    bogus = tmp_path / "pics.png"
    bogus.mkdir()  # a directory whose suffix routes to kind='image'

    result = _invoke_with_attach(bogus, tmp_path, monkeypatch)

    assert result.exit_code != 0
    assert "not a regular file" in result.output
    # The fail-fast happens BEFORE any disk side-effect: no orphan project
    # vault folder for code DIR should have been created.
    assert not (tmp_path / "vault").exists() or not list(
        (tmp_path / "vault").rglob("*dir*")
    )


def test_directory_image_ext_does_not_build_orphan_project(tmp_path: Path, monkeypatch):
    """Concretely: the project_dir for the code must not exist after the
    bad-attach kickoff bails."""
    bogus = tmp_path / "wireframe.jpg"
    bogus.mkdir()

    _invoke_with_attach(bogus, tmp_path, monkeypatch)

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    assert not vault.project_dir("DIR").exists()


def test_regular_image_file_still_accepted(tmp_path: Path, monkeypatch):
    """Guard against over-rejection: a real image FILE must still pass the
    fail-fast guard (it's only directories/FIFOs/devices that are rejected)."""
    img = tmp_path / "real.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")

    # The guard itself: a regular file is a file; a directory is not.
    assert img.is_file() and img.exists()
    assert cli._infer_attachment_kind(img) == "image"

    # And build_attachment accepts the regular image file (no crash).
    from modulatio.attachments import build_attachment

    att = build_attachment(img, kind="image")
    assert att.kind == "image"
    assert att.content is None
