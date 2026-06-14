# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Round-2 full-debug audit regressions for model_capabilities.

Two cost-routing defects:

1. Open-weights families (gemma / llama) baked ``cost_class='free-local'``
   into the family default, so a HOSTED (billed) instance of the same model
   misranked as cheapest in dispatch. Cost must flow from WHERE the model
   runs, not its family name.
2. ``_is_local_endpoint`` substring-matched 'localhost'/'127.0.0.1'/'0.0.0.0'/
   '::1' against the whole URL, so a remote host merely CONTAINING one of those
   tokens flipped to free-local.
"""

from __future__ import annotations

import pytest

from modulatio import model_capabilities as mc


# ── 1. open-weights families don't bake free-local ────────────────────────


@pytest.mark.parametrize("model", ["gemma-4-31b-it", "llama-3.3-70b-instruct"])
def test_open_weights_family_default_is_not_free_local(model):
    """A hosted open-weights model (no local base_url) must NOT infer
    free-local — that would misrank a billed model as cheapest in dispatch."""
    _, cost, _ = mc.infer(model)
    assert cost != "free-local"
    # Unknown-cost (None) is the conservative default; dispatch ranks it last.
    assert cost is None


def test_open_weights_hosted_endpoint_stays_paid_unknown():
    # google/gemma served via OpenRouter — billed, remote.
    _, cost, _ = mc.infer(
        "google/gemma-4-31b-it", base_url="https://openrouter.ai/api/v1"
    )
    assert cost != "free-local"


def test_open_weights_local_endpoint_still_free_local():
    """The local case (Ollama / LM Studio) must STILL promote to free-local."""
    _, cost, _ = mc.infer("gemma-4-31b-it", base_url="http://localhost:11434")
    assert cost == "free-local"
    _, cost2, _ = mc.infer("llama3.3", base_url="http://127.0.0.1:11434/v1")
    assert cost2 == "free-local"


# ── 2. _is_local_endpoint tests the hostname, not a substring ─────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://localhost:11434",
        "http://localhost:11434/v1",
        "https://127.0.0.1:1234/v1",
        "http://0.0.0.0:8080",
        "http://[::1]:8080/v1",
        "localhost:11434",  # bare host:port, no scheme
    ],
)
def test_is_local_endpoint_true_for_real_local_hosts(url):
    assert mc._is_local_endpoint(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost.evil-remote.example/v1",
        "https://api.0.0.0.0-host.net/v1",
        "https://127.0.0.1.attacker.example/v1",
        "https://my-localhost-proxy.cloud/v1",
        "https://openrouter.ai/api/v1",
        "https://api.anthropic.com",
    ],
)
def test_is_local_endpoint_false_for_remote_hosts_containing_token(url):
    assert mc._is_local_endpoint(url) is False


def test_is_local_endpoint_empty_is_false():
    assert mc._is_local_endpoint("") is False
    assert mc._is_local_endpoint(None) is False  # type: ignore[arg-type]


def test_remote_host_containing_localhost_does_not_force_free_local():
    """End-to-end: a paid hosted model whose URL merely contains 'localhost'
    must not be flipped to free-local."""
    _, cost, _ = mc.infer(
        "claude-opus-4-8", base_url="https://localhost.evil-remote.example/v1"
    )
    assert cost == "premium-cloud"  # unchanged from the family default
