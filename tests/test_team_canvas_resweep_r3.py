"""Round-3 re-sweep regressions for team_canvas — Finding 1 (security).

A producer holding a shell tool can plant a symlink inside the run's
artifacts/ tree pointing at a file OUTSIDE the tree (e.g. /etc/passwd or
another project's vault). Because rglob('*') filtered by is_file() follows
symlinks, the out-of-tree file's head-excerpt would otherwise be read and
injected into the NEXT producer's prompt context. These tests assert the
digest builder refuses to surface symlinked / escaped paths.
"""

from __future__ import annotations


from modulatio import team_canvas


def test_symlink_to_out_of_tree_file_is_not_read(tmp_path):
    """A symlink in artifacts/ pointing outside the tree must NOT leak the
    target's contents into the digest."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-VAULT-CONTENTS\nrow2\n", encoding="utf-8")

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    # A legitimate in-tree file so the digest is non-empty.
    (artifacts / "real.py").write_text("class Real:\n    pass\n", encoding="utf-8")
    # The planted escape symlink.
    link = artifacts / "leak.txt"
    link.symlink_to(secret)

    out = team_canvas.build_digest(artifacts)

    assert "TOP-SECRET-VAULT-CONTENTS" not in out
    assert "leak.txt" not in out
    # The honest in-tree file is still surfaced.
    assert "real.py" in out


def test_symlink_to_in_tree_file_is_skipped_not_duplicated(tmp_path):
    """Even a symlink whose target is INSIDE the tree is skipped (we keep the
    real file once; the symlink alias is dropped rather than read again)."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "engine.py").write_text("class Engine:\n    pass\n", encoding="utf-8")
    alias = artifacts / "alias.py"
    alias.symlink_to(artifacts / "engine.py")

    out = team_canvas.build_digest(artifacts)

    assert "engine.py" in out
    assert "alias.py" not in out


def test_symlinked_subdir_escape_is_blocked(tmp_path):
    """A symlinked DIRECTORY inside artifacts/ pointing out of tree must not
    expose files reached through it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "creds.env").write_text("API_KEY=sk-leaked\n", encoding="utf-8")

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "ok.md").write_text("# fine\n", encoding="utf-8")
    (artifacts / "escape").symlink_to(outside, target_is_directory=True)

    out = team_canvas.build_digest(artifacts)

    assert "API_KEY=sk-leaked" not in out
    assert "creds.env" not in out
    assert "ok.md" in out
