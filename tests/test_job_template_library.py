# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Brick B1b — Job Template library (index / search / checkout). The setup-side
companion to the skill library: the Leader searches THIS at job intake. Forked
near-verbatim from skill_library; cheap resident index (no interview bodies)."""

from __future__ import annotations

import pytest

from modulatio import job_template_library as jtl
from modulatio import job_templates as jt
from modulatio import vault


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(jt, "_JT_ROOT", tmp_path / "shared" / "job_templates")
    monkeypatch.setattr(jt, "_SEED_JT_ROOT", tmp_path / "seed" / "job_templates")
    return tmp_path


def _save(name, desc, caps=(), project_code=None):
    jt.save(jt.JobTemplate(name=name, description=desc, interview_body="# Interview\n" + "x" * 5000,
                           capability_preferences=caps), project_code=project_code)


def test_index_excludes_bodies(lib):
    _save("daily-essay", "A daily philosophy essay", ("long-form-writing",))
    idx = jtl.build_index()
    assert len(idx) == 1
    entry = idx[0]
    assert entry.name == "daily-essay"
    assert entry.description == "A daily philosophy essay"
    assert entry.capability_preferences == ("long-form-writing",)
    # The index row is a JobTemplateIndexEntry — it structurally has no body.
    assert not hasattr(entry, "interview_body")


def test_search_ranks_by_token_hits(lib):
    _save("weekly-brief", "Weekly competitor brief", ("web-research",))
    _save("daily-essay", "A daily philosophy essay", ("long-form-writing",))
    _save("research-dossier", "Deep research dossier on a topic", ("web-research",))
    hits = jtl.search_job_templates("research brief")
    assert hits[0].name == "weekly-brief"  # matches both 'brief' and (via tag) 'research'
    assert {h.name for h in hits} == {"weekly-brief", "research-dossier"}


def test_search_empty_query_returns_empty(lib):
    _save("x", "y")
    assert jtl.search_job_templates("") == []
    assert jtl.search_job_templates("   ") == []


def test_checkout_returns_full_jt_with_body(lib):
    _save("daily-essay", "A daily philosophy essay")
    jtt = jtl.checkout("daily-essay")
    assert jtt.name == "daily-essay"
    assert "Interview" in jtt.interview_body  # full body loaded on checkout


def test_checkout_unknown_is_empty(lib):
    assert jtl.checkout("nope").name == ""


def test_index_precedence_project_shadows_shared(tmp_path, monkeypatch):
    monkeypatch.setattr(jt, "_JT_ROOT", tmp_path / "shared" / "job_templates")
    monkeypatch.setattr(jt, "_SEED_JT_ROOT", tmp_path / "seed" / "job_templates")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    code = "PRJ"
    vault.init_project(code, "P", "o", exist_ok=True)
    _save("x", "SHARED desc")
    _save("x", "PROJECT desc", project_code=code)
    idx = {e.name: e for e in jtl.build_index(project_code=code)}
    assert idx["x"].description == "PROJECT desc"  # project wins
