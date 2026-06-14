# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""0.9.0 pre-ship re-sweep regressions for ``model_capabilities``.

Finding 1 (MEDIUM/correctness): ``_OPENAI_O_SERIES`` false-positived on
hyphen-/underscore-delimited o-tokens welded onto another family id
(``command-o4-beta``, ``llama-o3-instruct``, ``foo_o3_bar``). The old left
boundary class ``[/\\s_-]`` treated a bare ``-``/``_`` as a token boundary, so an
o-token embedded between two model-name segments was mistaken for an OpenAI
o-series id and tagged reasoning-heavy / premium-cloud. The fix narrows the LEFT
boundary to start-of-id / ``/`` / whitespace only, while keeping every genuine
o-series id matching.
"""

from __future__ import annotations

import pytest

from modulatio import model_capabilities as mc

# The exact signature ``infer`` returns for the OpenAI o-series.
_O_SERIES = ("reasoning-heavy", "premium-cloud", ("reasoning-heavy", "structured-output"))


@pytest.mark.parametrize(
    "model",
    [
        # The finding's own documented collision cases.
        "command-o4-beta",
        "llama-o3-instruct",
        # Underscore-welded variant the old class also wrongly matched.
        "foo_o3_bar",
    ],
)
def test_hyphen_welded_o_token_not_mis_tagged_openai(model):
    """An o-token glued onto a longer hyphenated/underscored family id must NOT
    resolve to the OpenAI o-series premium-reasoning signature."""
    assert mc.infer(model) != _O_SERIES, f"{model} was wrongly tagged OpenAI o-series"


@pytest.mark.parametrize(
    "model",
    [
        "o1",
        "o3",
        "o3-mini",
        "o4-mini",
        "o1-preview",
        "o1-2024-12-17",
        "openai/o3-mini",
    ],
)
def test_genuine_o_series_still_matched(model):
    """The narrowed boundary must not regress real o-series ids — bare and
    provider-prefixed forms still get the o-series signature."""
    tier, cost, caps = mc.infer(model)
    assert tier == "reasoning-heavy"
    assert cost == "premium-cloud"
    assert "reasoning-heavy" in caps


def test_whitespace_preceded_o_token_still_matched():
    """A whitespace boundary (e.g. via the label) still counts — only bare
    ``-``/``_`` was dropped from the left boundary class."""
    assert mc.infer("", label="some o1 model") == _O_SERIES
