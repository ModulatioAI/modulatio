"""Tests for `modulatio._crash` — the top-level CLI exception handler.

Covers:
  - Crash log is written to MODULATIO_CRASH_DIR with traceback + env basics
  - argv redaction strips secret-flagged values (inline `=value` and
    positional next-arg)
  - run_with_crash_handler returns 130 on KeyboardInterrupt without
    writing a log
  - run_with_crash_handler propagates SystemExit unchanged
  - run_with_crash_handler writes a log and returns 1 on plain Exception
  - run_with_crash_handler returns 0 on normal None-return success
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import _crash


def test_crash_dir_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODULATIO_CRASH_DIR", raising=False)
    assert _crash.crash_dir() == Path.home() / ".config" / "modulatio" / "crashes"


def test_crash_dir_honors_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path))
    assert _crash.crash_dir() == tmp_path


@pytest.mark.parametrize(
    "argv,expected",
    [
        # plain args: untouched
        (["modulatio", "kickoff", "--code", "FOO"], ["modulatio", "kickoff", "--code", "FOO"]),
        # inline =value: value stripped
        (
            ["modulatio", "--api-key=sk-deadbeef", "kickoff"],
            ["modulatio", "--api-key=<redacted>", "kickoff"],
        ),
        # positional value after secret flag: skipped
        (
            ["modulatio", "--token", "ghp_xxx", "kickoff"],
            ["modulatio", "--token", "<redacted>", "kickoff"],
        ),
        # multiple secret flags
        (
            ["modulatio", "--password=hunter2", "--bearer", "abc", "run"],
            ["modulatio", "--password=<redacted>", "--bearer", "<redacted>", "run"],
        ),
        # case-insensitive matching
        (["modulatio", "--API-KEY=secret"], ["modulatio", "--API-KEY=<redacted>"]),
        # composite name like --auth-header (matches `auth`)
        (["modulatio", "--auth-header=Bearer x"], ["modulatio", "--auth-header=<redacted>"]),
    ],
)
def test_redact_argv(argv: list[str], expected: list[str]) -> None:
    assert _crash._redact_argv(argv) == expected


@pytest.mark.parametrize(
    "argv,expected,leaked",
    [
        # secret embedded as a URL query param on a non-secret-named flag
        (
            ["modulatio", "--endpoint=https://h/v1?api_key=sk-deadbeef"],
            ["modulatio", "--endpoint=https://h/v1?api_key=<redacted>"],
            "sk-deadbeef",
        ),
        # inline token=... in an otherwise innocuous value
        (
            ["modulatio", "--config=token=ghp_abc123"],
            ["modulatio", "--config=token=<redacted>"],
            "ghp_abc123",
        ),
        # positional value (no flag) carrying an embedded secret
        (
            ["modulatio", "dsn=postgres://u/db?password=hunter2"],
            ["modulatio", "dsn=postgres://u/db?password=<redacted>"],
            "hunter2",
        ),
        # access_token query param mid-URL — value stops at the next `&`
        (
            ["modulatio", "--url=https://h?access_token=AAA&page=2"],
            ["modulatio", "--url=https://h?access_token=<redacted>&page=2"],
            "AAA",
        ),
        # case-insensitive embedded key
        (
            ["modulatio", "--x=Secret=topsecret"],
            ["modulatio", "--x=Secret=<redacted>"],
            "topsecret",
        ),
    ],
)
def test_redact_argv_scrubs_embedded_secret_values(
    argv: list[str], expected: list[str], leaked: str
) -> None:
    out = _crash._redact_argv(argv)
    assert out == expected
    assert leaked not in " ".join(out)


def test_redact_argv_embedded_secret_does_not_overscrub_plain_kv() -> None:
    # A non-secret key=value pair (e.g. code=FOO) must survive untouched.
    assert _crash._redact_argv(["modulatio", "--code=FOO", "page=2"]) == [
        "modulatio",
        "--code=FOO",
        "page=2",
    ]


def test_write_crash_log_creates_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path))
    try:
        raise ValueError("synthetic boom")
    except ValueError as exc:
        path = _crash.write_crash_log(exc, ["modulatio", "kickoff"])
    assert path.exists()
    assert path.parent == tmp_path
    assert path.name.startswith("crash-") and path.name.endswith(".log")
    body = path.read_text()
    assert "Modulatio crash report" in body
    assert "ValueError" in body
    assert "synthetic boom" in body
    assert "modulatio:" in body
    assert "python:" in body
    assert "platform:" in body
    assert "argv:      modulatio kickoff" in body


def test_write_crash_log_redacts_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path))
    try:
        raise RuntimeError("x")
    except RuntimeError as exc:
        path = _crash.write_crash_log(
            exc, ["modulatio", "--api-key=sk-secret", "models", "--token", "tok123"]
        )
    body = path.read_text()
    assert "sk-secret" not in body
    assert "tok123" not in body
    assert "<redacted>" in body


def test_write_crash_log_redacts_secret_in_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A secret echoed by str(exc) (URL query param / token=) must be
    scrubbed from the traceback body, not just from argv."""
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path))
    try:
        raise RuntimeError(
            "auth failed for https://api.host/v1?api_key=sk-deadbeef "
            "(token=ghp_topsecret)"
        )
    except RuntimeError as exc:
        path = _crash.write_crash_log(exc, ["modulatio", "kickoff"])
    body = path.read_text()
    assert "sk-deadbeef" not in body
    assert "ghp_topsecret" not in body
    # the shape is preserved so the log stays diagnosable
    assert "api_key=<redacted>" in body
    assert "token=<redacted>" in body


def test_write_crash_log_redacts_bearer_token_in_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A space-separated `Authorization: Bearer <token>` echoed in the
    exception must be scrubbed from the traceback body."""
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path))
    try:
        raise RuntimeError("401 with header Authorization: Bearer sk-bearerleak123")
    except RuntimeError as exc:
        path = _crash.write_crash_log(exc, ["modulatio", "kickoff"])
    body = path.read_text()
    assert "sk-bearerleak123" not in body
    assert "Bearer <redacted>" in body


def test_scrub_embedded_secrets_bearer_form() -> None:
    out = _crash._scrub_embedded_secrets("Authorization: Bearer abc.def-123")
    assert "abc.def-123" not in out
    assert "Bearer <redacted>" in out


def test_write_crash_log_prunes_old_logs_beyond_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The crash dir is capped — old logs are pruned after a write."""
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path))
    monkeypatch.setenv("MODULATIO_CRASH_KEEP", "3")
    # Seed five pre-existing (older) crash logs.
    for i in range(5):
        (tmp_path / f"crash-2000010{i}T000000_000000Z-{1000 + i}.log").write_text("old")
    # An unrelated file must survive the prune.
    (tmp_path / "README.txt").write_text("keep me")

    try:
        raise ValueError("boom")
    except ValueError as exc:
        newest = _crash.write_crash_log(exc, ["modulatio"])

    logs = sorted(tmp_path.glob("crash-*.log"))
    assert len(logs) == 3
    # The just-written log is the newest and must be retained.
    assert newest in logs
    assert (tmp_path / "README.txt").exists()


def test_write_crash_log_keep_defaults_when_env_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MODULATIO_CRASH_KEEP", "not-an-int")
    assert _crash._crash_keep() == _crash._DEFAULT_KEEP
    monkeypatch.setenv("MODULATIO_CRASH_KEEP", "0")
    # clamped to at least 1 so we never prune everything
    assert _crash._crash_keep() == 1


def test_run_with_crash_handler_returns_zero_on_success() -> None:
    assert _crash.run_with_crash_handler(lambda: None) == 0


def test_run_with_crash_handler_passes_through_int_return() -> None:
    assert _crash.run_with_crash_handler(lambda: 42) == 42


def test_run_with_crash_handler_returns_130_on_keyboard_interrupt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom() -> None:
        raise KeyboardInterrupt

    assert _crash.run_with_crash_handler(boom) == 130
    err = capsys.readouterr().err
    assert "Interrupted" in err


def test_run_with_crash_handler_propagates_system_exit() -> None:
    def boom() -> None:
        raise SystemExit(7)

    with pytest.raises(SystemExit) as excinfo:
        _crash.run_with_crash_handler(boom)
    assert excinfo.value.code == 7


def test_run_with_crash_handler_logs_and_returns_1_on_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path))

    def boom() -> None:
        raise ValueError("explode")

    rc = _crash.run_with_crash_handler(boom)
    assert rc == 1
    crash_logs = list(tmp_path.glob("crash-*.log"))
    assert len(crash_logs) == 1
    body = crash_logs[0].read_text()
    assert "ValueError" in body
    assert "explode" in body

    err = capsys.readouterr().err
    assert "Modulatio crashed" in err
    assert "ValueError" in err
    assert str(crash_logs[0]) in err
    assert "issues/new" in err


def test_run_with_crash_handler_handles_unwritable_log_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If the crash dir can't be created, still print the bug-report URL."""
    bad = tmp_path / "not-a-dir"
    bad.write_text("this is a file, not a directory")
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(bad / "deeper"))

    def boom() -> None:
        raise RuntimeError("x")

    rc = _crash.run_with_crash_handler(boom)
    assert rc == 1
    err = capsys.readouterr().err
    assert "Modulatio crashed" in err
    assert "could not write crash log" in err
    assert "issues/new" in err
