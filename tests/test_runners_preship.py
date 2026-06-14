"""0.9.0 PRE-SHIP regression tests for src/modulatio/runners.py.

Four findings fixed in this file; one regression apiece.

1. [MEDIUM/race] ``_rotated_pool_key`` second TOCTOU window — it checked
   ``pool_env_vars`` non-empty then re-derived the pool via
   ``next_pool_env_var``, which falls back to the BARE base var on an emptied
   pool. The base var may be PINNED → a pooled model could borrow a pinned key.
   Fix: snapshot the pool ONCE and index that snapshot (no second derivation).

2. [LOW/error-path] The responses() endpoint path skipped OAuth refresh AND
   429 pool failover (only completion() had them). Fix: mirror both onto the
   responses path via a shared refresh-kwargs helper + a failover loop.

3. [LOW/cost] The AuthenticationError refresh retry for a POOLED api_key preset
   re-used the refreshed BASE-var token (ApiKeyStrategy.refresh = re-read base
   var), which may be pinned. Fix: re-draw from the unpinned pool on the
   refresh-retry for a pooled preset.

4. [LOW/correctness] ``default_params`` could override the authoritative
   api_key for a NONE-auth preset (strategy emits no token, so nothing
   downstream re-asserts it). Fix: strip api_key/api_base from default_params
   so only the dedicated base_url/strategy fields set them.
"""

# ── Finding 4: default_params can't override authoritative auth ──────────────

def test_default_params_cannot_override_api_key_for_none_auth_preset(monkeypatch):
    """A none-auth preset whose ``default_params`` smuggles an api_key/api_base
    must NOT have those reach the resolved kwargs — the dedicated base_url +
    strategy token fields are authoritative, and a none-auth strategy emits no
    token, so nothing downstream would otherwise overwrite a smuggled key."""
    from modulatio.runners import _resolve_model_call_args

    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "local": {
                "label": "Local (no auth)",
                "base_url": "http://localhost:11434",
                "api_format": "openai",
                "auth_type": "none",
                "auth_config": {},
                "model": "llama3",
                "default_params": {
                    "extra_body": {"reasoning": {"enabled": False}},
                    "api_key": "sk-SMUGGLED-INTO-DEFAULT-PARAMS",
                    "api_base": "https://evil.invalid/v1",
                },
            }
        },
    )

    _model, kwargs = _resolve_model_call_args("local")
    # The tuning param survives; the smuggled auth fields do NOT.
    assert kwargs["extra_body"] == {"reasoning": {"enabled": False}}
    assert "api_key" not in kwargs  # none-auth → no key at all
    assert kwargs["api_base"] == "http://localhost:11434"  # dedicated field wins


def test_default_params_api_base_does_not_clobber_dedicated_base_url(monkeypatch):
    from modulatio.runners import _resolve_model_call_args

    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "p": {
                "base_url": "https://real.example/v1",
                "api_format": "openai",
                "auth_type": "none",
                "auth_config": {},
                "model": "m",
                "default_params": {"api_base": "https://wrong.invalid/v1"},
            }
        },
    )
    _model, kwargs = _resolve_model_call_args("p")
    assert kwargs["api_base"] == "https://real.example/v1"


# ── Finding 1: _rotated_pool_key TOCTOU — single snapshot, never base ────────

def test_rotated_pool_key_emptied_pool_returns_none_not_base(monkeypatch):
    """If the pool snapshot is empty, return None — NEVER the base var (which
    may be pinned). The old code only checked emptiness then re-derived the pool
    via ``next_pool_env_var``, which falls back to the base var if the pool
    emptied between the two reads."""
    from modulatio import provider_keys, runners

    runners._pool_rr_cursor.clear()
    monkeypatch.setattr(provider_keys, "pool_env_vars", lambda base: [])
    # If the code ever calls next_pool_env_var, it would fall back to base —
    # blow up so the test catches a regression to the two-derivation shape.
    def _boom(base):
        raise AssertionError("must not re-derive pool via next_pool_env_var")
    monkeypatch.setattr(provider_keys, "next_pool_env_var", _boom)

    assert runners._rotated_pool_key("SOME_BASE") is None


def test_rotated_pool_key_indexes_single_snapshot(monkeypatch):
    """The pool is read ONCE per call and indexed; no second derivation that
    could disagree with the emptiness check."""
    from modulatio import provider_keys, runners

    runners._pool_rr_cursor.clear()
    calls = {"n": 0}

    def _pool(base):
        calls["n"] += 1
        return ["K1", "K2"]

    monkeypatch.setattr(provider_keys, "pool_env_vars", _pool)
    monkeypatch.setattr(provider_keys, "next_pool_env_var",
                        lambda base: (_ for _ in ()).throw(
                            AssertionError("no re-derivation")))
    monkeypatch.setenv("K1", "val-1")
    monkeypatch.setenv("K2", "val-2")

    # Round-robin across the single snapshot.
    got = [runners._rotated_pool_key("B") for _ in range(3)]
    assert got == ["val-1", "val-2", "val-1"]
    # Exactly one pool derivation per call — never two.
    assert calls["n"] == 3


# ── Finding 3: AuthenticationError refresh on a pooled preset ────────────────

def _pooled_preset():
    return {
        "label": "Pooled", "base_url": "https://example.invalid/v1",
        "api_format": "openai", "auth_type": "api_key",
        "auth_config": {"env_var": "PRESHIP_POOL", "pool": True},
        "model": "meta/llama-3.1",
    }


def _fake_completion_response(content="ok"):
    class _U:
        prompt_tokens = 10
        completion_tokens = 5

    class _M:
        def __init__(self):
            self.content = content
            self.tool_calls = None

    class _C:
        def __init__(self):
            self.message = _M()

    class _R:
        def __init__(self):
            self.choices = [_C()]
            self.usage = _U()

    return _R()


def test_auth_refresh_retry_for_pooled_preset_redraws_from_pool(monkeypatch):
    """On a 401 for a pooled preset, the refresh-retry must draw from the
    UNPINNED pool (``_pooled_call_key``) — not the refreshed base-var token,
    which (for a pooled api_key strategy) is just a re-read of the base var and
    may be pinned to another model (metering-keel violation)."""
    import litellm
    from litellm.exceptions import AuthenticationError

    from modulatio import runners
    from modulatio.runners import litellm_runner

    preset = _pooled_preset()
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"pooled": preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset if k == "pooled" else None)
    monkeypatch.setattr(runners, "_pool_base", lambda model: "PRESHIP_POOL")
    # First call uses this pooled key; the refresh retry must use the pool too.
    monkeypatch.setattr(runners, "_pooled_call_key",
                        lambda pool_base, model: "POOL-KEY-OK")
    # _try_refresh_for returns the BASE var value (what ApiKeyStrategy.refresh
    # does) — a key that may be pinned. The fix must IGNORE it for pooled.
    monkeypatch.setattr(runners, "_try_refresh_for",
                        lambda model: "BASE-VAR-MAYBE-PINNED")

    seen = []

    def _completion(**kw):
        seen.append(kw.get("api_key"))
        if len(seen) == 1:
            raise AuthenticationError(message="401", llm_provider="x", model="m")
        return _fake_completion_response("ok")

    monkeypatch.setattr(litellm, "completion", _completion)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    out = litellm_runner("pooled")("hi")
    assert out == "ok"
    # Retry used the POOL key, never the (maybe-pinned) refreshed base var.
    assert seen == ["POOL-KEY-OK", "POOL-KEY-OK"]
    assert "BASE-VAR-MAYBE-PINNED" not in seen


# ── Finding 2: responses() path gains OAuth refresh + 429 failover ───────────

def _fake_responses_obj(text="resp-ok"):
    class _Content:
        def __init__(self):
            self.type = "output_text"
            self.text = text

    class _Item:
        def __init__(self):
            self.content = [_Content()]

    class _Resp:
        def __init__(self):
            self.output = [_Item()]
            self.usage = None

    return _Resp()


def _responses_preset():
    return {
        "label": "Responses", "base_url": "https://example.invalid/v1",
        "api_format": "openai", "auth_type": "oauth_xai",
        "auth_config": {},
        "model": "grok-4",
        "endpoint": "responses",
    }


def test_responses_path_refreshes_oauth_on_401(monkeypatch):
    """The responses() endpoint must attempt an OAuth refresh-once retry on a
    401, mirroring completion() — before the fix it just fired an alert and
    re-raised, leaving the xAI/o1 responses path unable to self-heal an expired
    token."""
    import litellm
    from litellm.exceptions import AuthenticationError

    from modulatio import runners
    from modulatio.runners import litellm_runner

    preset = _responses_preset()
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"r": preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset if k == "r" else None)
    monkeypatch.setattr(runners, "_pool_base", lambda model: None)
    monkeypatch.setattr(runners, "_try_refresh_for",
                        lambda model: "FRESH-OAUTH-TOKEN")

    seen = []

    def _responses(**kw):
        seen.append(kw.get("api_key"))
        if len(seen) == 1:
            raise AuthenticationError(message="401", llm_provider="x", model="m")
        return _fake_responses_obj("resp-ok")

    monkeypatch.setattr(litellm, "responses", _responses)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    out = litellm_runner("r")("hi")
    assert out == "resp-ok"
    assert seen[1] == "FRESH-OAUTH-TOKEN"  # retry used the refreshed token


def test_responses_path_429_pool_failover(monkeypatch):
    """The responses() endpoint must fail over across a key pool on a 429,
    mirroring completion()."""
    import litellm
    from litellm.exceptions import RateLimitError

    from modulatio import runners
    from modulatio.runners import litellm_runner

    preset = {
        "label": "Responses pooled", "base_url": "https://example.invalid/v1",
        "api_format": "openai", "auth_type": "api_key",
        "auth_config": {"env_var": "PRESHIP_RPOOL", "pool": True},
        "model": "grok-4", "endpoint": "responses",
    }
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"r": preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset if k == "r" else None)
    monkeypatch.setattr(runners, "_pool_base", lambda model: "PRESHIP_RPOOL")
    monkeypatch.setattr(runners, "_pool_count", lambda pool_base: 2)
    monkeypatch.setattr(runners, "_pooled_call_key",
                        lambda pool_base, model: "FIRST-KEY")
    rotated = iter(["SECOND-KEY"])
    monkeypatch.setattr(runners, "_rotated_pool_key",
                        lambda pool_base: next(rotated))

    seen = []

    def _responses(**kw):
        seen.append(kw.get("api_key"))
        if len(seen) == 1:
            raise RateLimitError(message="429", llm_provider="x", model="m")
        return _fake_responses_obj("resp-ok")

    monkeypatch.setattr(litellm, "responses", _responses)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    out = litellm_runner("r")("hi")
    assert out == "resp-ok"
    assert seen == ["FIRST-KEY", "SECOND-KEY"]
