# SPDX-License-Identifier: Apache-2.0
"""Re-sweep regression tests for setup_wizard.finalize (0.9.0 pre-ship).

Finding 1 [LOW]: a key staged under the neutral ``API_KEY`` sentinel
(``default_env_var_for``'s fallback for a malformed base_url) rendered a bogus
``api_key`` provider on the confirm line. ``API_KEY`` (7 chars) does not end
with ``_API_KEY`` (8 chars), so the suffix strip missed it. ``_derive_providers``
now skips the sentinel entirely.
"""

from __future__ import annotations

from modulatio.setup_wizard import finalize
from modulatio.setup_wizard.provider_step import default_env_var_for


def test_derive_providers_skips_neutral_api_key_sentinel():
    # The sentinel must contribute no provider name (not "api_key").
    state = {"staged_api_keys": {"API_KEY": "sk-x"}}
    assert finalize._derive_providers(state) == []


def test_derive_providers_skips_sentinel_case_insensitive():
    state = {"staged_api_keys": {"api_key": "sk-x", "Api_Key": "sk-y"}}
    assert finalize._derive_providers(state) == []


def test_derive_providers_sentinel_dropped_real_keys_kept():
    # A mixed bag: the sentinel is dropped, real providers survive.
    state = {
        "staged_api_keys": {
            "API_KEY": "sk-x",
            "OPENAI_API_KEY": "sk-y",
            "XAI_API_KEY": "sk-z",
        }
    }
    assert finalize._derive_providers(state) == ["openai", "xai"]


def test_sentinel_matches_default_env_var_for_fallback():
    # Guard the contract: the malformed-url fallback the wizard stages under is
    # exactly the sentinel finalize now skips, so the end-to-end path is clean.
    sentinel = default_env_var_for("not-a-url")
    assert sentinel == "API_KEY"
    state = {"staged_api_keys": {sentinel: "sk-x"}}
    assert finalize._derive_providers(state) == []
