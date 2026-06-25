# SPDX-License-Identifier: Apache-2.0
"""The release-time MDX→MD converter that builds the offline docs bundle from
the site's `.mdx` pages. Pure-logic — the file globbing/tar/manifest glue is not
tested here; the conversion is."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_docs_bundle.py"


def _load():
    spec = importlib.util.spec_from_file_location("build_docs_bundle", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MDX = """---
title: Agents
description: a one-line summary
---

import { Aside } from '@astrojs/starlight/components';

A team is a roster of **agents**.

<Aside type="note" title="Skills-first">
Task planning is the Leader's job.
</Aside>

## Choosing models
More text here.
"""


def test_converts_mdx_to_clean_markdown():
    bdb = _load()
    title, md = bdb.convert_mdx(MDX)

    assert title == "Agents"
    assert md.startswith("# Agents")          # title becomes the H1
    assert "---" not in md.splitlines()[0:3]  # frontmatter stripped
    assert "import {" not in md               # import line stripped
    assert "description:" not in md           # frontmatter key gone
    assert "<Aside" not in md and "</Aside>" not in md  # component gone
    assert "**Skills-first**" in md           # aside title -> bold
    assert "> Task planning is the Leader's job." in md  # aside body -> blockquote
    assert "## Choosing models" in md         # headings preserved
    assert "**agents**" in md                 # prose preserved
