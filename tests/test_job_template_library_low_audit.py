# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""LOW-audit regression for job_template_library.

Finding #63: the index entry ``.name`` was taken from frontmatter (``name:``)
and only fell back to the filename stem. A hand-authored JT file whose
frontmatter ``name`` diverges from its filename stem therefore produced a search
hit whose ``.name`` could NOT be checked out — ``checkout(name)`` resolves
``<name>.md`` strictly by filename stem. The entry name IS the checkout handle,
so the index must expose the stem.
"""

from __future__ import annotations

import pytest

from modulatio import job_template_library as jtl
from modulatio import job_templates as jt


@pytest.fixture
def lib(tmp_path, monkeypatch):
    shared = tmp_path / "shared" / "job_templates"
    monkeypatch.setattr(jt, "_JT_ROOT", shared)
    monkeypatch.setattr(jt, "_SEED_JT_ROOT", tmp_path / "seed" / "job_templates")
    shared.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_divergent_jt(lib_root):
    """Write a JT file whose filename stem ('philosophy-article') differs from
    its frontmatter ``name`` ('Philosophy Article') — as a hand-authored or
    externally-renamed file can."""
    path = jt._JT_ROOT / "philosophy-article.md"
    path.write_text(
        "---\n"
        "name: Philosophy Article\n"
        "description: A long-form philosophy article\n"
        "capability_preferences: long-form-writing\n"
        "---\n\n"
        "# Interview\n" + "x" * 5000 + "\n"
    )
    return path


def test_index_name_is_checkout_handle_not_frontmatter(lib):
    _write_divergent_jt(lib)
    idx = jtl.build_index()
    assert len(idx) == 1
    entry = idx[0]
    # The index name must be the stem (the checkout handle), NOT the frontmatter
    # display name — otherwise the round-trip below breaks.
    assert entry.name == "philosophy-article"


def test_search_hit_round_trips_to_checkout(lib):
    _write_divergent_jt(lib)
    hits = jtl.search_job_templates("philosophy article")
    assert hits, "expected a search hit for the divergent-name JT"
    entry = hits[0]
    # The whole point of the index: search -> name -> checkout that name.
    loaded = jtl.checkout(entry.name)
    assert loaded.name != "", (
        "search hit could not be checked out — index name diverged from the "
        "filename stem that checkout() resolves"
    )
    assert "Interview" in loaded.interview_body
