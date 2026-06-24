# SPDX-License-Identifier: Apache-2.0
"""Re-sweep R4 regression: attaching an oversized (or otherwise unreadable)
file must NOT crash the TUI.

``build_attachment`` raises ``ValueError`` when a file exceeds the active
size cap, and ``OSError`` (e.g. ``IsADirectoryError`` / permission) on an
unreadable path. The three TUI attach call sites previously caught only
``(FileNotFoundError, UnicodeDecodeError)``, so an oversized attachment let
the ``ValueError`` propagate out of ``attach_chat`` / ``attach_chat`` and
took down the screen. The fix broadens both ``except`` clauses to also catch
``ValueError`` and ``OSError`` and surfaces the message via the same escaped
status update used for the missing-file case.

The cap is forced tiny here via ``MODULATIO_MAX_ATTACHMENT_BYTES`` so a
trivially small fixture trips it (no need to write a real megabyte).
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from modulatio import roster, vault

PROJECT_CODE = "PR4"


@pytest.fixture
def project_with_roster(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "R4 attach fixture", "obj")
    roster.add_agent(
        project_code=PROJECT_CODE, agent_id="leader",
        name="Leader", identity="You are the Leader.",
        skills=["leader"], model="stub", tier="leader",
    )
    return tmp_path


async def test_oversized_kickoff_attachment_does_not_crash(
    project_with_roster, tmp_path, monkeypatch
):
    """An oversized kickoff attachment surfaces a status message instead of
    raising ValueError out of attach_chat and crashing the screen."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    # Force a 4-byte cap; the fixture below is 16 bytes → over the cap.
    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "4")
    big = tmp_path / "too_big.md"
    big.write_text("0123456789ABCDEF", encoding="utf-8")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        # Without the fix this raises ValueError and the test errors out.
        screen.attach_chat(big, kind="document")
        await pilot.pause()
        assert screen.chatbox_attachments == []  # nothing staged
        from textual.widgets import Static
        status = screen.query_one("#prompt-response", Static)
        assert "Attach failed" in str(status.render())


async def test_oversized_chat_attachment_does_not_crash(
    project_with_roster, tmp_path, monkeypatch
):
    """Symmetric: an oversized chat attachment is caught, not propagated."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "4")
    big = tmp_path / "huge.md"
    big.write_text("way over the cap", encoding="utf-8")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        screen.attach_chat(big, kind="document")
        await pilot.pause()
        assert screen.chatbox_attachments == []
        from textual.widgets import Static
        status = screen.query_one("#prompt-response", Static)
        assert "Attach failed" in str(status.render())


async def test_directory_attachment_does_not_crash(
    project_with_roster, tmp_path, monkeypatch
):
    """A directory path raises OSError (IsADirectoryError) from read_text; it
    too must be caught rather than crashing the TUI."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    a_dir = tmp_path / "a_directory"
    a_dir.mkdir()

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        screen.attach_chat(a_dir, kind="document")
        await pilot.pause()
        assert screen.chatbox_attachments == []
        from textual.widgets import Static
        status = screen.query_one("#prompt-response", Static)
        assert "Attach failed" in str(status.render())


def test_attach_chat_catches_value_and_os_errors():
    """Belt-and-suspenders source guard: the attach method broadens the except
    to ValueError + OSError (so the size cap / unreadable cases are handled),
    not just the original FileNotFoundError/UnicodeDecodeError."""
    from modulatio.tui.screens import prompt

    src = inspect.getsource(prompt.PromptScreen.attach_chat)
    assert "ValueError" in src
    assert "OSError" in src
