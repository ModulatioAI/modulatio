# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Model capability inference — the new home for a producer's capabilities.

Brick 2 of the skill-library arc. Once producers stop *holding skills*
(Brick 3), their capability surface — ``model_tier`` / ``cost_class`` /
``capability_tags`` — can no longer be derived from those skills. It has to
come from the **model** instead. A model's strengths are intrinsic to the
model, so this module infers a sensible default for known models from the
bare model id, and the setup wizard lets a human confirm or override it (the
"quick tag", surfaced in the Brick 3 wizard rewrite). Unknown / local models
fall back to a neutral default the human can correct.

This is heuristic and deliberately conservative: it only claims capabilities a
model family is broadly known to have. A wrong guess is corrected by the
explicit per-model tag stored on the preset (which always wins — see
:func:`modulatio.roster._caps_from_model`).

The capability vocabulary aligns with what dispatch already ranks
(``dispatch._TIER_RANK`` / ``dispatch._COST_RANK``); capability_tags are
free-form and mirror the ones the seed skills advertise.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

# Canonical tiers, weakest → strongest (matches dispatch._TIER_RANK).
MODEL_TIERS: tuple[str, ...] = (
    "budget", "generalist", "tactical", "tool-using", "reasoning-heavy", "strategic",
)

# Canonical cost classes, cheapest → dearest (matches dispatch._COST_RANK).
COST_CLASSES: tuple[str, ...] = ("free-local", "paid-cloud", "premium-cloud")

# Free-form capability tags a model can advertise. They describe model FIT for
# ANY deliverable class — code, long-form prose, marketing, data, research
# alike — never a task domain. Not exhaustive: a human can type anything in the
# wizard; these are the ones routing + the seed skills lean on, so the picker
# offers them.
CAPABILITY_TAGS: tuple[str, ...] = (
    "reasoning-heavy", "long-context", "vision", "structured-output",
    "code-production", "web-search", "fast",
)

# Neutral fallback for an unknown model — claims nothing special, costs
# nothing assumed. A human tags it accurately in the wizard.
_DEFAULT_TIER = "generalist"
_DEFAULT_CAPS: tuple[str, ...] = ()

# OpenAI o-series ids (``o1`` / ``o3`` / ``o4`` and their ``-mini`` / ``-preview``
# variants, optionally provider-prefixed like ``openai/o3-mini``). These tokens
# are far too short to match as bare substrings — ``o1`` etc. appear mid-word in
# unrelated ids (``mistralo1``, ``qwen2.5-o4b``, ``command-o4-beta``), which would
# wrongly tag them OpenAI reasoning/premium. Anchor to a token boundary instead:
# the token must start the id or follow a separator (``/`` ``-`` whitespace) and
# end the id or be followed by a separator — never embedded inside a longer word.
_OPENAI_O_SERIES = re.compile(r"(?:^|[/\s_-])o[134](?:$|[-\s/_])")

# Ordered (substrings, tier, cost_class, capability_tags). First family whose
# any-substring matches the lowercased model id (or label) wins. Order from
# most-specific to most-general within a vendor.
#
# cost_class is the model family's *default*. For OPEN-WEIGHTS families (gemma /
# llama) it is left ``None`` rather than baked to ``free-local``: the same
# open-weights model is free run locally but PAID via a hosted API
# (OpenRouter / NVIDIA / Ollama Cloud / Google). Cost must flow from WHERE the
# model runs, so ``_is_local_endpoint`` is the only thing that promotes a model
# to free-local; a hosted open-weights model stays unknown-cost (dispatch ranks
# unknown cost LAST, never misranking a billed model as cheapest).
_FAMILY_TABLE: tuple[tuple[tuple[str, ...], str, str | None, tuple[str, ...]], ...] = (
    # Anthropic
    (("opus",), "strategic", "premium-cloud",
     ("reasoning-heavy", "long-context", "vision", "structured-output")),
    (("sonnet",), "reasoning-heavy", "paid-cloud",
     ("reasoning-heavy", "long-context", "vision", "structured-output")),
    (("haiku",), "generalist", "paid-cloud", ("fast", "structured-output")),
    # OpenAI — note the o-series (o1/o3/o4) is matched separately in ``infer``
    # via ``_OPENAI_O_SERIES`` (boundary-anchored) since the bare two-char tokens
    # collide with unrelated ids as substrings. It is checked BEFORE this table so
    # it keeps its most-specific-within-vendor ordering.
    (("gpt-5", "gpt5"), "reasoning-heavy", "premium-cloud",
     ("reasoning-heavy", "long-context", "structured-output", "vision")),
    (("gpt-4", "gpt4"), "generalist", "paid-cloud",
     ("long-context", "structured-output", "vision")),
    # OpenAI open-weights (gpt-oss) — must precede nothing else it collides
    # with; matched on the distinct "gpt-oss" substring.
    (("gpt-oss",), "generalist", "paid-cloud",
     ("reasoning-heavy", "structured-output")),
    # xAI
    (("grok",), "reasoning-heavy", "paid-cloud",
     ("reasoning-heavy", "long-context", "web-search")),
    # NVIDIA Nemotron
    (("nemotron",), "reasoning-heavy", "paid-cloud",
     ("reasoning-heavy", "structured-output")),
    # DeepSeek
    (("deepseek",), "reasoning-heavy", "paid-cloud",
     ("reasoning-heavy", "code-production")),
    # Zhipu GLM
    (("glm",), "reasoning-heavy", "paid-cloud",
     ("reasoning-heavy", "structured-output")),
    # Moonshot Kimi
    (("kimi",), "reasoning-heavy", "paid-cloud",
     ("long-context", "reasoning-heavy")),
    # Qwen
    (("qwen",), "generalist", "paid-cloud",
     ("long-context", "code-production")),
    # Google Gemini / Gemma
    (("gemini",), "reasoning-heavy", "paid-cloud",
     ("long-context", "vision", "structured-output")),
    (("gemma",), "budget", None, ("fast",)),
    # Meta Llama
    (("llama",), "generalist", None, ()),
    # MiniMax
    (("minimax",), "generalist", "paid-cloud",
     ("code-production", "long-context")),
    # Mistral / Ministral — "ministral" before "mistral" so the lighter line
    # wins its own substring (neither contains the other, but keep them paired).
    (("ministral",), "budget", "paid-cloud", ("fast",)),
    (("mixtral", "mistral"), "generalist", "paid-cloud", ("structured-output",)),
)


def _is_local_endpoint(base_url: str) -> bool:
    """True only when ``base_url``'s HOSTNAME is the local machine.

    Tests the parsed hostname exactly — ``localhost`` (or a ``*.localhost``
    name) or a loopback / unspecified IP — never a bare substring. Substring
    matching wrongly flipped a remote host that merely *contains* one of these
    tokens (e.g. ``https://localhost.evil-remote.example`` or
    ``https://api.0.0.0.0-host.net``) to free-local.
    """
    raw = (base_url or "").strip()
    if not raw:
        return False
    # Default a scheme so a bare "host:port" still yields a hostname (urlsplit
    # otherwise reads "localhost:8080" as scheme="localhost", host="").
    parsed = urlsplit(raw if "//" in raw else f"//{raw}")
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def infer(
    model: str, label: str = "", base_url: str = ""
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Best-effort ``(model_tier, cost_class, capability_tags)`` for a model.

    Matches the model id (then the label) against known model families. A
    local endpoint (Ollama / LM Studio) forces ``cost_class = free-local``
    regardless of family, since the same open-weights model is free run
    locally but paid via a hosted API. Returns the neutral default for an
    unknown model — never raises, never guesses wildly.
    """
    hay = f"{model} {label}".lower()
    tier: str | None = _DEFAULT_TIER
    cost: str | None = None
    caps: tuple[str, ...] = _DEFAULT_CAPS
    if _OPENAI_O_SERIES.search(hay):
        # OpenAI o-series — most specific within the OpenAI vendor block.
        tier, cost, caps = (
            "reasoning-heavy",
            "premium-cloud",
            ("reasoning-heavy", "structured-output"),
        )
    else:
        for substrs, fam_tier, fam_cost, fam_caps in _FAMILY_TABLE:
            if any(s in hay for s in substrs):
                tier, cost, caps = fam_tier, fam_cost, fam_caps
                break
    if _is_local_endpoint(base_url):
        cost = "free-local"
    return tier, cost, caps


def infer_for_preset(preset: dict) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Convenience wrapper inferring from a model-preset dict's
    ``model`` / ``label`` / ``base_url`` fields."""
    return infer(
        preset.get("model", ""),
        preset.get("label", ""),
        preset.get("base_url", ""),
    )


__all__ = [
    "CAPABILITY_TAGS",
    "COST_CLASSES",
    "MODEL_TIERS",
    "infer",
    "infer_for_preset",
]
