# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Agnostic-sweep remediation: size GATES must be token-native (product-agnostic)
and export must not force document conversion (output-agnostic).

Modulatio's universal unit is the TOKEN and the deliverable is ANY artifact class.
A size gate measured in whitespace WORDS or CHARS silently breaks for compact
code/data/media; an export that only offers docx/pdf mangles non-prose.
"""
from __future__ import annotations

from pathlib import Path

from modulatio.dispatch_breaker import (
    _output_token_count,
    analyze_output,
    resolve_output_budget,
)


# ── dispatch_breaker: the cost/OOM backstop is token-native, not word-count ──

def test_output_token_count_does_not_undercount_compact_output():
    """A minified/whitespace-light blob has FEW words but MANY real tokens — the
    measure must reflect tokens (char/4), never the whitespace word count, or the
    runaway backstop fires far too late for non-prose families."""
    minified = '{"k":' + ",".join(str(i) for i in range(20000)) + "}"  # ~1 word
    assert len(minified.split()) <= 2                      # ~1 whitespace "word"
    toks = _output_token_count(minified)
    assert toks >= len(minified) // 4 - 1                  # token-native (char/4)
    assert toks > 10_000                                   # not collapsed to ~1


def test_hard_cap_backstop_fires_on_compact_blob():
    """A compact deliverable over the hard token cap still trips hard_cap — the
    backstop is no longer blind to whitespace-light output."""
    b = resolve_output_budget("producer")
    blob = "x" * (b.hard_cap * 4 + 8)        # ~hard_cap+ tokens, ~1 whitespace word
    abort = analyze_output(blob, blob, role="producer")
    assert abort is not None and abort.reason == "hard_cap"


# ── dispatch_breaker: no_commit gates on EMPTY, not a 40-char floor ──────────

def test_no_commit_allows_a_compact_valid_artifact():
    """A small-but-real committed deliverable (a one-line JSON / terse data
    answer) with verbose reasoning must NOT be discarded as no-progress."""
    reasoning = " ".join(f"w{i}" for i in range(4000))   # >> the no-commit-min
    committed = '{"value": 42}'                           # 13 chars — a real artifact
    assert analyze_output(reasoning, committed, role="producer") is None


def test_no_commit_still_trips_on_empty_commit():
    """A genuine empty (whitespace-only) commit after substantial output still
    trips the no-progress storm."""
    reasoning = " ".join(f"w{i}" for i in range(4000))
    abort = analyze_output(reasoning, "   \n  ", role="producer")
    assert abort is not None and abort.reason == "no_commit"


# ── export: a raw copy passthrough exists for non-document artifacts ─────────

def test_export_copy_is_a_raw_passthrough(tmp_path: Path):
    """The new 'copy' format copies any artifact class byte-for-byte — never
    through pandoc, which would mangle code/data."""
    from modulatio.export import export_artifact
    src = tmp_path / "module.py"
    src.write_text("def f():\n    return {'x': [1, 2, 3]}\n", encoding="utf-8")
    dest = tmp_path / "out.py"
    res = export_artifact(src, dest, format="copy")
    assert res.error is None
    assert dest.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


# ── output-agnostic: the draft fallback path is family-aware ──────────────────

def test_draft_fallback_name_is_family_aware():
    """A task with NO output_path must not always land at a document `.md` path:
    code/data/media families get a non-document extension so downstream
    extension-switching consumers don't mis-classify the deliverable."""
    from uuid import uuid4
    from modulatio.orchestration import _draft_fallback_name
    from modulatio.types import Task

    def t(kind, skills=None):
        return Task(id="T-1", project_id=uuid4(), goal_id="G", description="x",
                    artifact_kind=kind, required_skills=skills or [])
    assert _draft_fallback_name(t("document")) == "t-1.md"   # unchanged
    assert _draft_fallback_name(t("text")) == "t-1.md"       # default stays .md
    assert _draft_fallback_name(t("code")) == "t-1.txt"
    assert _draft_fallback_name(t("data")) == "t-1.json"
    assert _draft_fallback_name(t("image")) == "t-1.bin"
    # an explicit assembler skill overrides the artifact_kind
    assert _draft_fallback_name(t("text", ["code-assembly"])) == "t-1.txt"


def test_task_output_key_agrees_with_draft_fallback():
    """The wave-conflict key (_task_output_key) and the write fallback must use
    the SAME family-aware extension, or write+lookup diverge."""
    from uuid import uuid4
    from modulatio.orchestration import Orchestrator, _draft_fallback_name
    from modulatio.types import Task
    code_task = Task(id="T-2", project_id=uuid4(), goal_id="G", description="x",
                     artifact_kind="code")
    assert Orchestrator._task_output_key(code_task) == f"drafts/{_draft_fallback_name(code_task)}"
    assert Orchestrator._task_output_key(code_task) == "drafts/t-2.txt"  # not .md
