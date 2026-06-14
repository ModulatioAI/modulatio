"""Round-2 full-debug audit regressions for ``modulatio.tools``.

Filed in a uniquely-named module (not test_tools.py) to avoid colliding
with a sibling agent editing the same suite concurrently.
"""

from __future__ import annotations

from pathlib import Path

from modulatio import tools


def _make_artifacts(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    art.mkdir()
    return art


def test_run_shell_binary_child_output_does_not_crash(tmp_path):
    """A child that emits non-UTF-8/binary bytes on stdout must NOT raise
    ``UnicodeDecodeError`` out of ``run_shell`` and discard all output.

    ``subprocess.Popen(..., text=True)`` decodes child output as UTF-8;
    the default 'strict' decoder raises ``UnicodeDecodeError`` inside
    ``communicate()`` on undecodable bytes — and that exception is caught
    by neither the TimeoutExpired nor the FileNotFoundError handler, so it
    would propagate and crash the chat loop. The fix passes
    ``errors="replace"`` so the model still gets a best-effort body.

    Before the fix this test errors with UnicodeDecodeError; after, it
    returns a normal result body.
    """
    art = _make_artifacts(tmp_path)
    # Write raw invalid-UTF-8 bytes (0xFF 0xFE 0x80) straight to stdout's
    # binary buffer, then a sentinel marker so we can confirm the surrounding
    # output survives the replacement.
    (art / "emit_binary.py").write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe\\x80')\n"
        "sys.stdout.buffer.write(b'SENTINEL')\n"
        "sys.stdout.buffer.flush()\n"
    )
    rs = tools.make_run_shell(art)
    # Must not raise; must return a result string with the decodable portion.
    out = rs(cmd="python3 emit_binary.py", profile="full", timeout=10)
    assert "exit_code: 0" in out
    assert "SENTINEL" in out
    # The undecodable bytes were substituted, not dropped wholesale.
    assert "�" in out
