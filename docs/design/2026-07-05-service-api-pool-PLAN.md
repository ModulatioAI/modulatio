# Service-API Pool (SERVICES) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Config-tab storage for outside SaaS/API keys with floating-pool JIT checkout, capability-class tools (`generate_image`, `generate_video`, `generate_speech`, `research_search`) + a generic `api_call`, riding the existing metered tier — per spec `docs/design/2026-07-05-service-api-pool.md`.

**Architecture:** New `services.py` (registry + capability resolution + key checkout via the existing `provider_keys` slot pool), `service_catalog.py` (shipped vendor entries), `service_tools.py` (capability tools + vendor adapters + `api_call`, merged into `tools.build_registry`). Metering reuses `build_metered_authorizer` with a new `allowed_keys` forgiveness for schema-declared param names; `orchestration._run_chat_loop` gains the (currently missing) authorizer wiring. TUI gets a SERVICES section in `ConfigScreen`; doctor gets service checks.

**Tech Stack:** Python stdlib only (urllib via `tools._urlopen`, json, base64). No new dependencies. pytest + monkeypatch fixtures, no live API calls.

---

## House conventions (every task, every agent)

- **Disciplines ride along:** name the operation, verify by observed reality (run the test, re-read the file); code minimalism (reuse seams, YAGNI); ruff-clean; match surrounding idiom; don't reformat untouched lines.
- Tests: `python -m pytest tests/<file> -x -q` (NEVER bare `pytest` — cwd/sys.path lesson). Full gates before final commit: `ruff check src/ tests/` AND `python -m pytest tests/ -q`.
- **NO git stash, ever.** Sequential commits only, one per task, on local `main`. Do not push.
- All service keys/values in tests are fakes (`"sk-test-xyz"`); never a real key in a test or fixture.

## File structure

| File | Responsibility |
|---|---|
| Create `src/modulatio/services.py` | Service registry: `services.json` load/save/add/remove, capability→service resolution, key checkout from `provider_keys` slots, doctor report |
| Create `src/modulatio/service_catalog.py` | Static shipped catalog: vendor entries (id, name, capabilities, env_var, base_url, auth_shape, beta) |
| Create `src/modulatio/service_tools.py` | The tools: `api_call`, `generate_image`, `research_search`, `generate_speech`, `generate_video`; vendor adapters; auth injection; binary save |
| Modify `src/modulatio/tools.py` (`build_registry`) | Merge service tools (lazy import — avoids the circular import) |
| Modify `src/modulatio/metered.py` | `allowed_keys` param on `build_metered_authorizer` |
| Modify `src/modulatio/orchestration.py` (`_run_chat_loop`) | Build + thread the metered authorizer dispatcher (today: nothing wires it — metered tools are dormant-denied) |
| Modify `src/modulatio/comptroller.py` | `set_budget_field()` writer for the TUI Set-budget action |
| Create `src/modulatio/_seed_skills/{generate-images,generate-video,generate-speech,research-via-api,service-api-call}.md` | Seed skills declaring the tool loadouts (resolve live via `skills.py:296` package fallback) |
| Modify `src/modulatio/tui/screens/configuration.py` | SERVICES section under PROVIDERS & KEYS |
| Modify `src/modulatio/cli.py` (`_run_doctor_checks`) | Service doctor checks |
| Modify `src/modulatio/_docs/26-tools.md`, `CHANGELOG.md` | Durable docs (the "all the fixin's" rule) |
| Tests | `tests/test_services.py`, `tests/test_service_catalog.py`, `tests/test_service_tools.py`, `tests/test_service_metering.py`, `tests/test_services_screen.py` |

---

### Task 1: `services.py` — registry, resolution, checkout

**Files:**
- Create: `src/modulatio/services.py`
- Test: `tests/test_services.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the service-API pool registry (spec 2026-07-05-service-api-pool)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modulatio import provider_keys, services
from modulatio.services import Service


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(services, "SERVICES_FILE", tmp_path / "services.json")
    monkeypatch.setattr(
        provider_keys, "LABELS_FILE", tmp_path / "key_labels.json"
    )
    monkeypatch.setattr(provider_keys, "PINS_FILE", tmp_path / "key_pins.json")


def _svc(**over) -> Service:
    base = dict(
        id="tavily", name="Tavily", kind="catalog",
        capabilities=("research",), env_var="TAVILY_API_KEY",
        base_url="https://api.tavily.com", auth_shape="bearer",
        free_tier=False, docs_url="", per_task_cap=1,
    )
    base.update(over)
    return Service(**base)


def test_add_and_load_round_trip():
    services.add_service(_svc())
    loaded = services.load_services()
    assert loaded["tavily"].env_var == "TAVILY_API_KEY"
    assert loaded["tavily"].capabilities == ("research",)


def test_add_custom_requires_base_url():
    with pytest.raises(ValueError):
        services.add_service(_svc(id="mine", kind="custom", base_url=""))


def test_remove_service():
    services.add_service(_svc())
    assert services.remove_service("tavily") is True
    assert services.load_services() == {}
    assert services.remove_service("tavily") is False


def test_resolve_for_capability_only_one_configured():
    services.add_service(_svc())
    assert services.resolve_for_capability("research").id == "tavily"
    assert services.resolve_for_capability("image") is None


def test_resolve_for_capability_default_wins():
    services.add_service(_svc())
    services.add_service(_svc(id="other", name="Other",
                              env_var="OTHER_API_KEY"))
    # Two services back the capability, no default → ambiguous → None.
    assert services.resolve_for_capability("research") is None
    services.set_capability_default("research", "other")
    assert services.resolve_for_capability("research").id == "other"


def test_checkout_key_first_set_slot(monkeypatch):
    services.add_service(_svc())
    monkeypatch.setenv("TAVILY_API_KEY_2", "sk-test-slot2")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert services.checkout_key(_svc()) == "sk-test-slot2"


def test_checkout_key_none_when_unset(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert services.checkout_key(_svc()) is None


def test_corrupt_services_json_degrades_to_empty(tmp_path: Path):
    services.SERVICES_FILE.write_text("{not json", encoding="utf-8")
    assert services.load_services() == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_services.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'modulatio.services'` (or ImportError).

- [ ] **Step 3: Implement `src/modulatio/services.py`**

```python
"""The service-API pool — outside-SaaS services and their keys.

Spec: docs/design/2026-07-05-service-api-pool.md. A *service* is an outside
application reachable over an API (image generation, video, speech, research,
anything within reason). The registry (``services.json`` in the config dir)
records which services the operator configured; the KEYS live in the existing
``provider_keys`` numbered-slot pool under each service's ``env_var`` (vault
``.env`` via ``config.set_env_secret`` — this module never stores a secret).

Resolution is the model-picker rhyme: the operator's per-capability default,
else the only service configured for that capability, else None (ambiguity
does not guess). The Leader is a superset consumer — same registry, no
Leader-specific path.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Optional

from modulatio import config, provider_keys

SERVICES_FILE = config.CONFIG_DIR / "services.json"

#: Capability classes seeded by the spec. The set is OPEN — a new class earns
#: a typed tool when a cataloged vendor demands one; until then the custom
#: lane (api_call + a Leader-authored skill) serves it.
KNOWN_CAPABILITIES = ("image", "video", "speech", "research")

_AUTH_SHAPES = ("bearer", "header:", "query:")


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    kind: str  # "catalog" | "custom"
    capabilities: tuple[str, ...]
    env_var: str
    base_url: str
    auth_shape: str  # "bearer" | "header:<Name>" | "query:<name>"
    free_tier: bool = False
    docs_url: str = ""
    per_task_cap: int = 1


def _validate(svc: Service) -> None:
    if not svc.id or not svc.id.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"service id {svc.id!r} must be a simple slug")
    if svc.kind not in ("catalog", "custom"):
        raise ValueError(f"service kind {svc.kind!r} must be catalog|custom")
    if not svc.base_url.startswith(("https://", "http://")):
        raise ValueError(
            f"service {svc.id!r}: base_url is required and must be an "
            "absolute http(s) URL — it is pinned at add time (the pin IS "
            "the authorization)"
        )
    if not (svc.auth_shape == "bearer"
            or svc.auth_shape.startswith(("header:", "query:"))):
        raise ValueError(
            f"service {svc.id!r}: auth_shape {svc.auth_shape!r} must be "
            "bearer | header:<Name> | query:<name>"
        )
    if not svc.env_var.isidentifier():
        raise ValueError(f"service {svc.id!r}: env_var {svc.env_var!r} invalid")


def _load_raw() -> dict:
    if not SERVICES_FILE.exists():
        return {}
    try:
        data = json.loads(
            SERVICES_FILE.read_text(encoding="utf-8", errors="replace")
        )
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_raw(data: dict) -> None:
    config.write_secret_file(SERVICES_FILE, json.dumps(data, indent=2))


def load_services() -> dict[str, Service]:
    out: dict[str, Service] = {}
    for sid, entry in _load_raw().get("services", {}).items():
        try:
            out[sid] = Service(
                id=sid,
                name=str(entry.get("name", sid)),
                kind=str(entry.get("kind", "custom")),
                capabilities=tuple(entry.get("capabilities", ())),
                env_var=str(entry.get("env_var", "")),
                base_url=str(entry.get("base_url", "")),
                auth_shape=str(entry.get("auth_shape", "bearer")),
                free_tier=bool(entry.get("free_tier", False)),
                docs_url=str(entry.get("docs_url", "")),
                per_task_cap=int(entry.get("per_task_cap", 1)),
            )
        except (TypeError, ValueError):
            continue  # one corrupt entry must not hide the rest
    return out


def get_service(service_id: str) -> Optional[Service]:
    return load_services().get(service_id)


def add_service(svc: Service) -> None:
    _validate(svc)
    data = _load_raw()
    entry = asdict(svc)
    entry.pop("id")
    entry["capabilities"] = list(svc.capabilities)
    data.setdefault("services", {})[svc.id] = entry
    _save_raw(data)


def remove_service(service_id: str) -> bool:
    data = _load_raw()
    if service_id not in data.get("services", {}):
        return False
    del data["services"][service_id]
    # Drop any capability default pointing at the removed service.
    defaults = data.get("capability_defaults", {})
    for cap in [c for c, s in defaults.items() if s == service_id]:
        del defaults[cap]
    _save_raw(data)
    return True


def set_capability_default(capability: str, service_id: str) -> None:
    data = _load_raw()
    if service_id not in data.get("services", {}):
        raise ValueError(f"no service {service_id!r} configured")
    data.setdefault("capability_defaults", {})[capability] = service_id
    _save_raw(data)


def resolve_for_capability(capability: str) -> Optional[Service]:
    """The model-picker rhyme: operator default → only-one-configured →
    None. Two candidates and no default is ambiguous — never guess with
    someone else's money."""
    all_svcs = load_services()
    default_id = _load_raw().get("capability_defaults", {}).get(capability)
    if default_id and default_id in all_svcs:
        if capability in all_svcs[default_id].capabilities:
            return all_svcs[default_id]
    backed = [s for s in all_svcs.values() if capability in s.capabilities]
    return backed[0] if len(backed) == 1 else None


def checkout_key(svc: Service) -> Optional[str]:
    """Check a key out of the service's slot pool: first set slot wins
    (v1 floor — on-error rotation is the adapter retry's job, deferred).
    Returns the key VALUE; callers inject it and never surface it."""
    for slot in provider_keys.list_keys(svc.env_var):
        if slot["is_set"]:
            value = os.environ.get(slot["env_var"], "").strip()
            if value:
                return value
    return None


def cost_class_for(svc: Service) -> Optional[str]:
    """Metered by default; ``free_tier`` opts out (spec Decision 3)."""
    return None if svc.free_tier else "paid-cloud"
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_services.py -x -q`
Expected: PASS (8 tests). NOTE: `provider_keys.PINS_FILE`/`LABELS_FILE` names — verify against `provider_keys.py:45-77` before running; if the module constants differ, patch the real names.

- [ ] **Step 5: Ruff + commit**

```bash
ruff check src/modulatio/services.py tests/test_services.py
git add src/modulatio/services.py tests/test_services.py
git commit -m "Services S1: the service registry — resolution + slot-pool checkout"
```

---

### Task 2: `service_catalog.py` — shipped vendor entries

**Files:**
- Create: `src/modulatio/service_catalog.py`
- Test: `tests/test_service_catalog.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Shipped service catalog — vendor entries the SERVICES tab offers."""
from __future__ import annotations

from modulatio import service_catalog
from modulatio.services import Service


def test_catalog_entries_are_valid_services():
    entries = service_catalog.catalog()
    assert entries, "catalog must ship at least one entry"
    for e in entries:
        assert isinstance(e.service, Service)
        assert e.service.kind == "catalog"
        assert e.service.base_url.startswith("https://")
        assert e.service.capabilities


def test_catalog_lookup_by_id():
    e = service_catalog.entry("tavily")
    assert e is not None
    assert "research" in e.service.capabilities
    assert service_catalog.entry("nope") is None


def test_catalog_covers_seed_capabilities():
    caps = {c for e in service_catalog.catalog()
            for c in e.service.capabilities}
    assert {"image", "video", "speech", "research"} <= caps
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_service_catalog.py -x -q`
Expected: FAIL — ModuleNotFoundError.

- [ ] **Step 3: Implement `src/modulatio/service_catalog.py`**

```python
"""The shipped service catalog — known vendors the SERVICES tab offers.

Mirrors ``provider_catalog``'s role for LLM providers. Content is each
vendor's GENERAL current offering (Modulatio ships for every user, never one
user's setup). ``beta=True`` = the API shape is catalog-documented but not
yet live-verified; the TUI surfaces the flag. The catalog earns entries over
time — the custom lane (api_call) covers the long tail from day one.
"""
from __future__ import annotations

from dataclasses import dataclass

from modulatio.services import Service


@dataclass(frozen=True)
class CatalogEntry:
    service: Service
    beta: bool = True
    notes: str = ""


_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        service=Service(
            id="openai-images", name="OpenAI Images", kind="catalog",
            capabilities=("image",), env_var="OPENAI_API_KEY",
            base_url="https://api.openai.com", auth_shape="bearer",
            docs_url="https://platform.openai.com/docs/guides/images",
        ),
        beta=True, notes="gpt-image-1 via /v1/images/generations (b64 result)",
    ),
    CatalogEntry(
        service=Service(
            id="tavily", name="Tavily Search", kind="catalog",
            capabilities=("research",), env_var="TAVILY_API_KEY",
            base_url="https://api.tavily.com", auth_shape="bearer",
            docs_url="https://docs.tavily.com",
            per_task_cap=3,
        ),
        beta=True, notes="POST /search — ranked results with content snippets",
    ),
    CatalogEntry(
        service=Service(
            id="elevenlabs", name="ElevenLabs", kind="catalog",
            capabilities=("speech",), env_var="ELEVENLABS_API_KEY",
            base_url="https://api.elevenlabs.io",
            auth_shape="header:xi-api-key",
            docs_url="https://elevenlabs.io/docs",
        ),
        beta=True, notes="POST /v1/text-to-speech/<voice_id> (mp3 bytes)",
    ),
    CatalogEntry(
        service=Service(
            id="luma", name="Luma Dream Machine", kind="catalog",
            capabilities=("video",), env_var="LUMAAI_API_KEY",
            base_url="https://api.lumalabs.ai", auth_shape="bearer",
            docs_url="https://docs.lumalabs.ai",
        ),
        beta=True,
        notes="POST /dream-machine/v1/generations then poll by id",
    ),
)


def catalog() -> tuple[CatalogEntry, ...]:
    return _CATALOG


def entry(service_id: str) -> "CatalogEntry | None":
    for e in _CATALOG:
        if e.service.id == service_id:
            return e
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_service_catalog.py -x -q` — Expected: PASS.

- [ ] **Step 5: Ruff + commit**

```bash
ruff check src/modulatio/service_catalog.py tests/test_service_catalog.py
git add src/modulatio/service_catalog.py tests/test_service_catalog.py
git commit -m "Services S2: the shipped catalog — image/video/speech/research seeds (beta-flagged)"
```

---

### Task 3: `metered.py` — `allowed_keys` forgiveness

**Files:**
- Modify: `src/modulatio/metered.py` (`build_metered_authorizer`, ~line 143)
- Test: `tests/test_service_metering.py`

The narrow-params scan (`_scan_for_network_params`) denies key-NAME hits (`method` is in `FORBIDDEN_ARG_KEYS`) and URL-like VALUES. Service tools declare their params by schema; the authorizer must forgive **schema-declared top-level key names only** — URL-like values and nested/over-depth hits stay denied (the fail-closed backstop the reviewers demanded).

- [ ] **Step 1: Write the failing tests** (start `tests/test_service_metering.py`)

```python
"""Metered-tier behavior for service tools (allowed_keys + wiring)."""
from __future__ import annotations

import pytest

from modulatio import metered


def _authorize(args: dict, allowed: tuple[str, ...]):
    fn = metered.build_metered_authorizer(
        project_code="SVC", cost_class="paid-cloud", tool_name="api_call",
        task_id="T-1", agent_id="a1", pinned_units=[],
        artifacts_root=None, allowed_keys=allowed,
    )
    return fn("api_call", args)


def test_allowed_keys_forgives_declared_top_level_names(monkeypatch):
    # Reaches the comptroller (budget check) only if the scan passed;
    # stub it so the test isolates the scan.
    monkeypatch.setattr(
        metered.comptroller, "authorize_metered_tool",
        lambda *a, **k: metered.comptroller.Authorization(
            allowed=True, refresh_at=None, reason="ok"),
    )
    ok, reason = _authorize(
        {"service": "tavily", "method": "POST", "path": "search"},
        allowed=("service", "method", "path", "params", "json"),
    )
    assert ok, reason


def test_allowed_keys_never_forgives_url_like_values():
    ok, reason = _authorize(
        {"service": "tavily", "method": "GET",
         "path": "https://evil.example/x"},
        allowed=("service", "method", "path"),
    )
    assert not ok and "forbidden network param" in reason


def test_allowed_keys_never_forgives_nested_hits():
    ok, reason = _authorize(
        {"service": "t", "method": "GET", "path": "x",
         "params": {"callback_url": "later"}},
        allowed=("service", "method", "path", "params"),
    )
    assert not ok


def test_no_allowed_keys_keeps_old_behavior():
    ok, reason = _authorize({"method": "GET"}, allowed=())
    assert not ok
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_service_metering.py -x -q`
Expected: FAIL — `build_metered_authorizer() got an unexpected keyword argument 'allowed_keys'`.

- [ ] **Step 3: Implement.** In `build_metered_authorizer`, add the keyword and the forgiveness filter right after the scan:

```python
def build_metered_authorizer(
    *,
    project_code: str,
    cost_class: str | None,
    tool_name: str,
    task_id: str,
    agent_id: str,
    pinned_units: "list[Task]",
    artifacts_root: "Path",
    per_task_cap: int = 1,
    allowed_keys: tuple[str, ...] = (),
) -> "Callable[[str, dict], tuple[bool, str]]":
```

and inside `authorize`, replace the `bad = _scan_for_network_params(args)` block's use with:

```python
        bad = _scan_for_network_params(args)
        if allowed_keys and bad:
            # A service tool allowlists its options BY SCHEMA (the reason
            # string below has always asked for exactly this): forgive
            # TOP-LEVEL declared key NAMES only. URL-like values, nested
            # hits, and over-depth stay denied — fail-closed backstop.
            allowed = {k.lower() for k in allowed_keys}
            bad = [
                p for p in bad
                if "." in p or "<" in p or p.lower() not in allowed
            ]
        if bad:
```

(the existing `return (False, f"metered tool ... forbidden network param(s) ...")` body is unchanged).

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_service_metering.py -x -q` — Expected: PASS (4 tests).
Then the metered regression suite: `python -m pytest tests/ -q -k "metered"` — Expected: PASS (no behavior change when `allowed_keys` is absent).

- [ ] **Step 5: Ruff + commit**

```bash
ruff check src/modulatio/metered.py tests/test_service_metering.py
git add src/modulatio/metered.py tests/test_service_metering.py
git commit -m "Services S3: metered allowed_keys — schema-declared names pass the narrow-param scan"
```

---

### Task 4: `service_tools.py` part 1 — auth injection + `api_call`

**Files:**
- Create: `src/modulatio/service_tools.py`
- Test: `tests/test_service_tools.py`

- [ ] **Step 1: Write the failing tests** (start `tests/test_service_tools.py`)

```python
"""Service capability tools — api_call, generation, research."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from modulatio import provider_keys, service_tools, services
from modulatio.services import Service


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(services, "SERVICES_FILE", tmp_path / "services.json")
    monkeypatch.setattr(provider_keys, "LABELS_FILE", tmp_path / "l.json")
    monkeypatch.setattr(provider_keys, "PINS_FILE", tmp_path / "p.json")


class _FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, content_type="application/json"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire_service(monkeypatch, **over):
    svc = dict(
        id="myapi", name="My API", kind="custom",
        capabilities=("research",), env_var="MYAPI_API_KEY",
        base_url="https://api.example.com", auth_shape="bearer",
    )
    svc.update(over)
    services.add_service(Service(**svc))
    monkeypatch.setenv("MYAPI_API_KEY", "sk-test-xyz")


def test_api_call_joins_pinned_base_and_injects_bearer(monkeypatch):
    _wire_service(monkeypatch)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    out = service_tools.api_call(service="myapi", method="GET",
                                 path="/v1/things?q=x")
    assert seen["url"] == "https://api.example.com/v1/things?q=x"
    assert seen["auth"] == "Bearer sk-test-xyz"
    assert '"ok"' in out


def test_api_call_key_never_in_result(monkeypatch):
    _wire_service(monkeypatch)
    monkeypatch.setattr(
        service_tools, "_urlopen",
        lambda req, timeout=None: _FakeResponse(b'{"ok": true}'))
    out = service_tools.api_call(service="myapi", path="/x")
    assert "sk-test-xyz" not in out


def test_api_call_denies_absolute_path(monkeypatch):
    _wire_service(monkeypatch)
    out = service_tools.api_call(service="myapi",
                                 path="https://evil.example/x")
    assert "must be relative" in out


def test_api_call_unknown_service_lists_configured(monkeypatch):
    _wire_service(monkeypatch)
    out = service_tools.api_call(service="nope", path="/x")
    assert "myapi" in out and "No service" in out


def test_api_call_missing_key_names_the_fix(monkeypatch):
    _wire_service(monkeypatch)
    monkeypatch.delenv("MYAPI_API_KEY")
    out = service_tools.api_call(service="myapi", path="/x")
    assert "no API key" in out and "SERVICES" in out


def test_api_call_header_and_query_auth_shapes(monkeypatch):
    _wire_service(monkeypatch, id="hdr", env_var="HDR_API_KEY",
                  auth_shape="header:X-Api-Key")
    monkeypatch.setenv("HDR_API_KEY", "sk-test-h")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["hdr"] = req.get_header("X-api-key")
        seen["url"] = req.full_url
        return _FakeResponse(b"{}")

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    service_tools.api_call(service="hdr", path="/x")
    assert seen["hdr"] == "sk-test-h"

    _wire_service(monkeypatch, id="qry", env_var="QRY_API_KEY",
                  auth_shape="query:key")
    monkeypatch.setenv("QRY_API_KEY", "sk-test-q")
    service_tools.api_call(service="qry", path="/x")
    assert "key=sk-test-q" in seen["url"]


def test_api_call_posts_json_body(monkeypatch):
    _wire_service(monkeypatch)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = req.data
        seen["method"] = req.get_method()
        return _FakeResponse(b"{}")

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    service_tools.api_call(service="myapi", method="POST", path="/x",
                           json={"q": "hello"})
    assert seen["method"] == "POST"
    assert json.loads(seen["body"]) == {"q": "hello"}


def test_api_call_http_error_reported_not_raised(monkeypatch):
    import urllib.error
    _wire_service(monkeypatch)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 402, "Payment Required", {},
            io.BytesIO(b'{"error": "quota"}'))

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    out = service_tools.api_call(service="myapi", path="/x")
    assert "402" in out and "quota" in out
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_service_tools.py -x -q` → ModuleNotFoundError.

- [ ] **Step 3: Implement `src/modulatio/service_tools.py`** (part 1)

```python
"""Capability tools for the service-API pool.

Spec: docs/design/2026-07-05-service-api-pool.md. Tools are named for what
they DO (generate_image, research_search, ...) — a thin adapter per cataloged
vendor; ``api_call`` is the custom-service generic. The key is checked out of
the slot pool and injected HERE, at the adapter layer: it never appears in
agent context, tool results, or errors. Binary results are written into the
artifacts tree and returned as a PATH, never bytes.

The pinned ``base_url`` (operator-approved at add time) is the authorization
for ``api_call``'s network target — absolute URLs in args are refused, so the
model can never choose a host (the http_get discipline, service-shaped).
"""
from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from modulatio import services
from modulatio.services import Service
from modulatio.tools import _cap_http_body, _urlopen

_DEFAULT_TIMEOUT = 30.0
_MAX_TIMEOUT = 120.0


def _no_service_msg(capability: str) -> str:
    return (
        f"No {capability} service configured (or several with no default) — "
        "the operator adds/picks one under Config → SERVICES."
    )


def _no_key_msg(svc: Service) -> str:
    return (
        f"Service {svc.id!r} has no API key set — the operator adds one "
        f"under Config → SERVICES → Manage keys ({svc.env_var})."
    )


def _apply_auth(
    svc: Service, key: str, url: str, headers: dict[str, str]
) -> str:
    """Inject the checked-out key per the service's auth shape. Returns the
    (possibly query-extended) URL; mutates headers in place."""
    if svc.auth_shape == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif svc.auth_shape.startswith("header:"):
        headers[svc.auth_shape.split(":", 1)[1]] = key
    elif svc.auth_shape.startswith("query:"):
        name = svc.auth_shape.split(":", 1)[1]
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode({name: key})}"
    return url


def _service_request(
    svc: Service,
    key: str,
    method: str,
    url: str,
    json_body: Optional[dict],
    timeout: float,
) -> "tuple[int, bytes, str]":
    """One authenticated HTTP round-trip. Returns (status, body, content_type).
    HTTPError is caught and returned as its status + body — an API error is a
    tool RESULT the model recovers from, not a crash (http_get's contract)."""
    headers: dict[str, str] = {"Accept": "application/json"}
    url = _apply_auth(svc, key, url, headers)
    data = None
    if json_body is not None:
        data = _json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=data, headers=headers, method=method.upper()
    )
    try:
        with _urlopen(req, timeout=timeout) as resp:
            ctype = str(resp.headers.get("Content-Type", ""))
            return int(getattr(resp, "status", 200)), resp.read(), ctype
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except OSError:
            pass
        return int(exc.code), body, ""


def api_call(
    service: str,
    method: str = "GET",
    path: str = "",
    params: Optional[dict] = None,
    json: Optional[dict] = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    """Call a configured service's API, relative to its pinned base URL."""
    svc = services.get_service(str(service))
    if svc is None:
        have = ", ".join(sorted(services.load_services())) or "(none)"
        return (
            f"No service {service!r} configured — configured services: "
            f"{have}. The operator adds services under Config → SERVICES."
        )
    p = str(path)
    if "://" in p or p.startswith("//"):
        return (
            f"api_call path must be relative to the service's pinned base "
            f"URL ({svc.base_url}) — got an absolute URL."
        )
    key = services.checkout_key(svc)
    if key is None:
        return (
            f"Service {svc.id!r} has no API key set (no API key in any "
            f"{svc.env_var} slot) — the operator adds one under Config → "
            "SERVICES → Manage keys."
        )
    url = svc.base_url.rstrip("/") + "/" + p.lstrip("/")
    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(params)}"
    if urllib.parse.urlparse(url).netloc != urllib.parse.urlparse(
        svc.base_url
    ).netloc:
        return "api_call path escaped the pinned base URL host — refused."
    timeout = min(max(float(timeout), 1.0), _MAX_TIMEOUT)
    status, body, _ctype = _service_request(
        svc, key, str(method), url, json, timeout
    )
    text = body.decode("utf-8", errors="replace")
    text = text.replace(key, "[REDACTED]")  # belt: key can never echo back
    head = f"HTTP {status}\n" if status >= 400 else ""
    return head + _cap_http_body(text, over_read=False)
```

Note for the implementer: check `_cap_http_body`'s real signature at `tools.py:196` before wiring (`over_read` kwarg) — if it differs, match the real one and adjust the call.

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_service_tools.py -x -q` → PASS (8 tests).

- [ ] **Step 5: Ruff + commit**

```bash
ruff check src/modulatio/service_tools.py tests/test_service_tools.py
git add src/modulatio/service_tools.py tests/test_service_tools.py
git commit -m "Services S4: api_call — pinned-base generic with auth injection, key never echoed"
```

---

### Task 5: generation + research tools (adapters)

**Files:**
- Modify: `src/modulatio/service_tools.py`
- Test: `tests/test_service_tools.py` (append)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_service_tools.py`)

```python
def _wire_capability(monkeypatch, capability, service_id, env_var,
                     base_url, auth_shape="bearer"):
    services.add_service(Service(
        id=service_id, name=service_id, kind="catalog",
        capabilities=(capability,), env_var=env_var, base_url=base_url,
        auth_shape=auth_shape))
    monkeypatch.setenv(env_var, "sk-test-xyz")


def test_generate_image_openai_saves_binary(tmp_path, monkeypatch):
    _wire_capability(monkeypatch, "image", "openai-images",
                     "OPENAI_API_KEY", "https://api.openai.com")
    import base64
    png = b"\x89PNG-fake-bytes"
    resp = _json_bytes = json.dumps(
        {"data": [{"b64_json": base64.b64encode(png).decode()}]}
    ).encode()
    monkeypatch.setattr(service_tools, "_urlopen",
                        lambda req, timeout=None: _FakeResponse(resp))
    written = []
    gen = service_tools.make_generate_image(
        artifacts_root=tmp_path, on_artifact_write=written.append)
    out = gen(prompt="a lighthouse", filename="light.png")
    saved = tmp_path / "light.png"
    assert saved.read_bytes() == png
    assert written == [saved]
    assert "light.png" in out and "sk-test" not in out


def test_generate_image_no_service_configured(tmp_path):
    gen = service_tools.make_generate_image(
        artifacts_root=tmp_path, on_artifact_write=None)
    assert "SERVICES" in gen(prompt="x")


def test_generate_image_rejects_path_separators(tmp_path, monkeypatch):
    _wire_capability(monkeypatch, "image", "openai-images",
                     "OPENAI_API_KEY", "https://api.openai.com")
    gen = service_tools.make_generate_image(
        artifacts_root=tmp_path, on_artifact_write=None)
    out = gen(prompt="x", filename="../escape.png")
    assert not (tmp_path.parent / "escape.png").exists()


def test_research_search_tavily_formats_results(monkeypatch):
    _wire_capability(monkeypatch, "research", "tavily",
                     "TAVILY_API_KEY", "https://api.tavily.com")
    resp = json.dumps({"results": [
        {"title": "T1", "url": "https://a.example", "content": "alpha"},
        {"title": "T2", "url": "https://b.example", "content": "beta"},
    ]}).encode()
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        return _FakeResponse(resp)

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    out = service_tools.research_search(query="modulatio")
    assert seen["url"].endswith("/search")
    assert seen["body"]["query"] == "modulatio"
    assert "T1" in out and "https://b.example" in out


def test_generate_speech_elevenlabs_saves_mp3(tmp_path, monkeypatch):
    _wire_capability(monkeypatch, "speech", "elevenlabs",
                     "ELEVENLABS_API_KEY", "https://api.elevenlabs.io",
                     auth_shape="header:xi-api-key")
    mp3 = b"ID3-fake-audio"
    monkeypatch.setattr(
        service_tools, "_urlopen",
        lambda req, timeout=None: _FakeResponse(mp3, "audio/mpeg"))
    gen = service_tools.make_generate_speech(
        artifacts_root=tmp_path, on_artifact_write=None)
    out = gen(text="howdy", filename="howdy.mp3")
    assert (tmp_path / "howdy.mp3").read_bytes() == mp3


def test_generate_video_luma_polls_then_downloads(tmp_path, monkeypatch):
    _wire_capability(monkeypatch, "video", "luma",
                     "LUMAAI_API_KEY", "https://api.lumalabs.ai")
    calls = []
    vid = b"fake-mp4-bytes"

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if req.get_method() == "POST":
            return _FakeResponse(b'{"id": "gen-1", "state": "queued"}')
        if "gen-1" in req.full_url:
            state = "completed" if len(calls) >= 3 else "dreaming"
            return _FakeResponse(json.dumps({
                "id": "gen-1", "state": state,
                "assets": {"video": "https://cdn.lumalabs.ai/v/gen-1.mp4"},
            }).encode())
        return _FakeResponse(vid, "video/mp4")

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    monkeypatch.setattr(service_tools, "_POLL_INTERVAL_SECONDS", 0.0)
    gen = service_tools.make_generate_video(
        artifacts_root=tmp_path, on_artifact_write=None)
    out = gen(prompt="a storm", filename="storm.mp4")
    assert (tmp_path / "storm.mp4").read_bytes() == vid
    assert "storm.mp4" in out


def test_generate_video_poll_timeout_names_job(tmp_path, monkeypatch):
    _wire_capability(monkeypatch, "video", "luma",
                     "LUMAAI_API_KEY", "https://api.lumalabs.ai")

    def fake_urlopen(req, timeout=None):
        if req.get_method() == "POST":
            return _FakeResponse(b'{"id": "gen-9", "state": "queued"}')
        return _FakeResponse(b'{"id": "gen-9", "state": "dreaming"}')

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    monkeypatch.setattr(service_tools, "_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(service_tools, "_POLL_WALL_CAP_SECONDS", 0.0)
    gen = service_tools.make_generate_video(
        artifacts_root=tmp_path, on_artifact_write=None)
    out = gen(prompt="x", filename="x.mp4")
    assert "gen-9" in out and "timed out" in out
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_service_tools.py -x -q` → FAIL (`make_generate_image` missing).

- [ ] **Step 3: Implement** (append to `service_tools.py`)

```python
import time

_POLL_INTERVAL_SECONDS = 5.0
_POLL_WALL_CAP_SECONDS = 480.0  # video jobs run minutes; cap hard


def _save_media(
    artifacts_root: Path,
    filename: str,
    data: bytes,
    on_artifact_write: "Callable[[Path], None] | None",
) -> Path:
    """Write binary bytes under the artifacts root. Filename is flattened to
    its basename — a tool result must never place a file outside the tree."""
    safe = Path(str(filename)).name or "service-output.bin"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    path = artifacts_root / safe
    path.write_bytes(data)
    if on_artifact_write is not None:
        on_artifact_write(path)
    return path


def _resolve(capability: str) -> "tuple[Service, str] | str":
    """Service + key for a capability, or the operator-facing error string."""
    svc = services.resolve_for_capability(capability)
    if svc is None:
        return _no_service_msg(capability)
    key = services.checkout_key(svc)
    if key is None:
        return _no_key_msg(svc)
    return (svc, key)


# ── image ──────────────────────────────────────────────────────────────────

def _openai_image(svc, key, prompt, size, timeout):
    status, body, _ = _service_request(
        svc, key, "POST",
        svc.base_url.rstrip("/") + "/v1/images/generations",
        {"model": "gpt-image-1", "prompt": prompt, "size": size, "n": 1},
        timeout,
    )
    if status >= 400:
        return None, f"HTTP {status}: {body.decode('utf-8', 'replace')[:500]}"
    import base64
    try:
        b64 = _json.loads(body)["data"][0]["b64_json"]
        return base64.b64decode(b64), ""
    except (KeyError, IndexError, ValueError) as exc:
        return None, f"unexpected response shape: {exc}"


_IMAGE_ADAPTERS = {"openai-images": _openai_image}


def make_generate_image(
    artifacts_root: Path,
    on_artifact_write: "Callable[[Path], None] | None",
) -> Callable[..., str]:
    def generate_image(
        prompt: str,
        size: str = "1024x1024",
        filename: str = "generated-image.png",
        timeout: float = _DEFAULT_TIMEOUT,
        **_: object,
    ) -> str:
        got = _resolve("image")
        if isinstance(got, str):
            return got
        svc, key = got
        adapter = _IMAGE_ADAPTERS.get(svc.id)
        if adapter is None:
            return (
                f"Service {svc.id!r} has no image adapter — use api_call "
                "with its documented endpoint (see the service's skill)."
            )
        data, err = adapter(svc, key, str(prompt), str(size),
                            min(float(timeout), _MAX_TIMEOUT))
        if data is None:
            return f"generate_image ({svc.id}) failed — {err}"
        path = _save_media(artifacts_root, filename, data, on_artifact_write)
        return (
            f"Image generated by {svc.name} and saved to {path.name} "
            f"({len(data)} bytes). Reference it by that artifact filename."
        )
    return generate_image


# ── research ───────────────────────────────────────────────────────────────

def _tavily_search(svc, key, query, max_results, timeout):
    status, body, _ = _service_request(
        svc, key, "POST", svc.base_url.rstrip("/") + "/search",
        {"query": query, "max_results": max_results}, timeout,
    )
    if status >= 400:
        return f"HTTP {status}: {body.decode('utf-8', 'replace')[:500]}"
    try:
        results = _json.loads(body).get("results", [])
    except ValueError:
        return "unexpected non-JSON response"
    lines = [
        f"{i}. {r.get('title', '')}\n   {r.get('url', '')}\n"
        f"   {str(r.get('content', ''))[:400]}"
        for i, r in enumerate(results, 1)
    ]
    return "\n".join(lines) or "No results."


_RESEARCH_ADAPTERS = {"tavily": _tavily_search}


def research_search(
    query: str, max_results: int = 5, timeout: float = _DEFAULT_TIMEOUT,
    **_: object,
) -> str:
    got = _resolve("research")
    if isinstance(got, str):
        return got
    svc, key = got
    adapter = _RESEARCH_ADAPTERS.get(svc.id)
    if adapter is None:
        return (
            f"Service {svc.id!r} has no research adapter — use api_call "
            "with its documented endpoint."
        )
    n = max(1, min(int(max_results), 12))
    out = adapter(svc, key, str(query), n,
                  min(float(timeout), _MAX_TIMEOUT))
    return _cap_http_body(out.replace(key, "[REDACTED]"), over_read=False)


# ── speech ─────────────────────────────────────────────────────────────────

def _elevenlabs_speech(svc, key, text, voice, timeout):
    status, body, _ = _service_request(
        svc, key, "POST",
        svc.base_url.rstrip("/") + f"/v1/text-to-speech/{voice}",
        {"text": text, "model_id": "eleven_multilingual_v2"}, timeout,
    )
    if status >= 400:
        return None, f"HTTP {status}: {body.decode('utf-8', 'replace')[:500]}"
    return body, ""


_SPEECH_ADAPTERS = {"elevenlabs": _elevenlabs_speech}


def make_generate_speech(
    artifacts_root: Path,
    on_artifact_write: "Callable[[Path], None] | None",
) -> Callable[..., str]:
    def generate_speech(
        text: str,
        voice: str = "21m00Tcm4TlvDq8ikWAM",  # vendor's default demo voice
        filename: str = "generated-speech.mp3",
        timeout: float = _DEFAULT_TIMEOUT,
        **_: object,
    ) -> str:
        got = _resolve("speech")
        if isinstance(got, str):
            return got
        svc, key = got
        adapter = _SPEECH_ADAPTERS.get(svc.id)
        if adapter is None:
            return (
                f"Service {svc.id!r} has no speech adapter — use api_call."
            )
        data, err = adapter(svc, key, str(text), str(voice),
                            min(float(timeout), _MAX_TIMEOUT))
        if data is None:
            return f"generate_speech ({svc.id}) failed — {err}"
        path = _save_media(artifacts_root, filename, data, on_artifact_write)
        return (
            f"Speech generated by {svc.name} and saved to {path.name} "
            f"({len(data)} bytes)."
        )
    return generate_speech


# ── video (submit-then-poll) ───────────────────────────────────────────────

def _luma_video(svc, key, prompt, timeout):
    """Submit, poll to terminal state under the wall cap, download the asset.
    Returns (bytes, "") or (None, error-with-job-id) so a denied/late retry
    can be JUDGED rather than blindly re-spent."""
    status, body, _ = _service_request(
        svc, key, "POST",
        svc.base_url.rstrip("/") + "/dream-machine/v1/generations",
        {"prompt": prompt}, timeout,
    )
    if status >= 400:
        return None, f"submit HTTP {status}: {body.decode('utf-8', 'replace')[:500]}"
    try:
        job = _json.loads(body)
        job_id = str(job["id"])
    except (ValueError, KeyError) as exc:
        return None, f"unexpected submit response: {exc}"
    deadline = time.monotonic() + _POLL_WALL_CAP_SECONDS
    while True:
        status, body, _ = _service_request(
            svc, key, "GET",
            svc.base_url.rstrip("/")
            + f"/dream-machine/v1/generations/{job_id}",
            None, timeout,
        )
        if status >= 400:
            return None, f"poll HTTP {status} (job {job_id})"
        try:
            job = _json.loads(body)
        except ValueError:
            return None, f"unexpected poll response (job {job_id})"
        state = str(job.get("state", ""))
        if state == "completed":
            break
        if state == "failed":
            return None, f"vendor reported failed (job {job_id})"
        if time.monotonic() >= deadline:
            return None, (
                f"timed out after {int(_POLL_WALL_CAP_SECONDS)}s waiting on "
                f"job {job_id} — it may still complete vendor-side"
            )
        time.sleep(_POLL_INTERVAL_SECONDS)
    asset = str(job.get("assets", {}).get("video", ""))
    if not asset.startswith("https://"):
        return None, f"no video asset on completed job {job_id}"
    status, data, _ = _service_request(svc, key, "GET", asset, None, timeout)
    if status >= 400:
        return None, f"asset download HTTP {status} (job {job_id})"
    return data, ""


_VIDEO_ADAPTERS = {"luma": _luma_video}


def make_generate_video(
    artifacts_root: Path,
    on_artifact_write: "Callable[[Path], None] | None",
) -> Callable[..., str]:
    def generate_video(
        prompt: str,
        filename: str = "generated-video.mp4",
        timeout: float = _DEFAULT_TIMEOUT,
        **_: object,
    ) -> str:
        got = _resolve("video")
        if isinstance(got, str):
            return got
        svc, key = got
        adapter = _VIDEO_ADAPTERS.get(svc.id)
        if adapter is None:
            return f"Service {svc.id!r} has no video adapter — use api_call."
        data, err = adapter(svc, key, str(prompt),
                            min(float(timeout), _MAX_TIMEOUT))
        if data is None:
            return f"generate_video ({svc.id}) failed — {err}"
        path = _save_media(artifacts_root, filename, data, on_artifact_write)
        return (
            f"Video generated by {svc.name} and saved to {path.name} "
            f"({len(data)} bytes)."
        )
    return generate_video
```

The `_luma_video` asset download goes to the vendor-returned CDN URL — that URL came from the authenticated job response, not the model, so the pinned-base rule is not violated (note this in the module docstring if the reviewer asks).

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_service_tools.py -x -q` → PASS.

- [ ] **Step 5: Ruff + commit**

```bash
ruff check src/modulatio/service_tools.py tests/test_service_tools.py
git add -u && git add tests/test_service_tools.py
git commit -m "Services S5: capability tools — image/research/speech/video adapters, binary-to-artifact"
```

---

### Task 6: registry merge — `tools.build_registry` includes service tools

**Files:**
- Modify: `src/modulatio/tools.py` (`build_registry`, ~line 1784)
- Modify: `src/modulatio/service_tools.py` (add `build_service_tools`)
- Test: `tests/test_service_tools.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
def test_build_registry_includes_service_tools_when_configured(
        tmp_path, monkeypatch):
    from modulatio import tools
    _wire_capability(monkeypatch, "image", "openai-images",
                     "OPENAI_API_KEY", "https://api.openai.com")
    reg = tools.build_registry(artifacts_root=tmp_path)
    assert "generate_image" in reg
    assert "api_call" in reg
    assert reg["generate_image"].cost_class == "paid-cloud"
    # capabilities with no configured service stay OUT (opt-in shape)
    assert "generate_video" not in reg


def test_build_registry_free_tier_service_unmetered(tmp_path, monkeypatch):
    from modulatio import tools
    _wire_capability(monkeypatch, "research", "tavily",
                     "TAVILY_API_KEY", "https://api.tavily.com")
    services.remove_service("tavily")
    services.add_service(Service(
        id="tavily", name="Tavily", kind="catalog",
        capabilities=("research",), env_var="TAVILY_API_KEY",
        base_url="https://api.tavily.com", auth_shape="bearer",
        free_tier=True))
    reg = tools.build_registry(artifacts_root=tmp_path)
    assert reg["research_search"].cost_class is None


def test_build_registry_no_services_no_service_tools(tmp_path):
    from modulatio import tools
    reg = tools.build_registry(artifacts_root=tmp_path)
    assert "api_call" not in reg
    assert "generate_image" not in reg
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_service_tools.py -x -q` → FAIL (KeyError / not-in).

- [ ] **Step 3: Implement.** In `service_tools.py`, add:

```python
def build_service_tools(
    artifacts_root: "Path | None",
    on_artifact_write: "Callable[[Path], None] | None" = None,
) -> "dict[str, object]":
    """Service tools for ``tools.build_registry`` — one Tool per capability
    that has a resolvable service, plus ``api_call`` when ANY service is
    configured. Nothing configured → empty dict (the run_shell opt-in shape).
    cost_class comes from the backing service (metered by default,
    ``free_tier`` opts out)."""
    from modulatio.tools import Tool  # local: tools imports us lazily too

    out: dict[str, object] = {}
    all_svcs = services.load_services()
    if not all_svcs:
        return out
    out["api_call"] = Tool(
        name="api_call",
        description=(
            "Call a configured outside service's API, relative to its "
            "operator-pinned base URL. Use the service's skill for its "
            "endpoint shapes. Args: service (id), method, path (relative), "
            "params (query dict), json (body dict)."
        ),
        call=api_call,
        params_schema={
            "type": "object",
            "properties": {
                "service": {"type": "string",
                            "description": "Configured service id."},
                "method": {"type": "string",
                           "description": "GET|POST|PUT|PATCH|DELETE."},
                "path": {"type": "string",
                         "description": "Path relative to the pinned base."},
                "params": {"type": "object",
                           "description": "Query parameters."},
                "json": {"type": "object",
                         "description": "JSON request body."},
                "timeout": {"type": "number"},
            },
            "required": ["service", "path"],
        },
        cost_class=(
            None
            if all(s.free_tier for s in all_svcs.values())
            else "paid-cloud"
        ),
    )
    caps: "dict[str, tuple[str, str, Callable[..., str], dict]]" = {}
    root = artifacts_root if artifacts_root is not None else Path(".")
    img = services.resolve_for_capability("image")
    if img is not None:
        caps["generate_image"] = (img.id, services.cost_class_for(img),
            make_generate_image(root, on_artifact_write),
            {"type": "object", "properties": {
                "prompt": {"type": "string",
                           "description": "What to depict."},
                "size": {"type": "string",
                         "description": "e.g. 1024x1024."},
                "filename": {"type": "string",
                             "description": "Artifact filename (basename)."},
            }, "required": ["prompt"]})
    res = services.resolve_for_capability("research")
    if res is not None:
        caps["research_search"] = (res.id, services.cost_class_for(res),
            research_search,
            {"type": "object", "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer",
                                "description": "1-12, default 5."},
            }, "required": ["query"]})
    spc = services.resolve_for_capability("speech")
    if spc is not None:
        caps["generate_speech"] = (spc.id, services.cost_class_for(spc),
            make_generate_speech(root, on_artifact_write),
            {"type": "object", "properties": {
                "text": {"type": "string"},
                "voice": {"type": "string"},
                "filename": {"type": "string"},
            }, "required": ["text"]})
    vid = services.resolve_for_capability("video")
    if vid is not None:
        caps["generate_video"] = (vid.id, services.cost_class_for(vid),
            make_generate_video(root, on_artifact_write),
            {"type": "object", "properties": {
                "prompt": {"type": "string"},
                "filename": {"type": "string"},
            }, "required": ["prompt"]})
    for name, (svc_id, cost, fn, schema) in caps.items():
        out[name] = Tool(
            name=name,
            description=(
                f"{name.replace('_', ' ').capitalize()} via the configured "
                f"{svc_id!r} service. Binary results are saved into the "
                "artifacts tree and returned as a filename."
            ),
            call=fn, params_schema=schema, cost_class=cost,
        )
    return out
```

In `tools.build_registry`, just before `return registry` (after the run_shell/read_tool_result opt-in blocks), add:

```python
    # Service-API pool (spec 2026-07-05): capability tools + api_call for
    # operator-configured outside services. Lazy import — service_tools
    # imports tools at module level (Tool/_urlopen), so the reverse edge
    # must stay inside the function body.
    from modulatio import service_tools as _service_tools
    registry.update(_service_tools.build_service_tools(
        artifacts_root=artifacts_root,
        on_artifact_write=on_artifact_write,
    ))
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_service_tools.py tests/test_llm_with_tools.py -q` → PASS (registry regressions included).

- [ ] **Step 5: Ruff + commit**

```bash
ruff check src/modulatio/tools.py src/modulatio/service_tools.py tests/test_service_tools.py
git add -u
git commit -m "Services S6: registry merge — service tools ride build_registry (opt-in shape)"
```

---

### Task 7: metered wiring — `_run_chat_loop` builds the authorizer

**Files:**
- Modify: `src/modulatio/orchestration.py` (`_run_chat_loop`, ~line 5295; the `run_llm_with_tools` call ~line 5421)
- Test: `tests/test_service_metering.py` (append)

Today NOTHING passes `metered_authorizer` into `run_llm_with_tools` — any metered tool in a loadout is denied ("no spend authorizer wired"). This task wires it: one `build_metered_authorizer` per metered tool in the loadout (the name guard demands one per tool), dispatched by called-name.

- [ ] **Step 1: Write the failing tests**

```python
def test_chat_loop_authorizes_metered_service_tool(tmp_path, monkeypatch):
    """End-to-end through run_llm_with_tools: a metered tool + a budget →
    the call is authorized and executes (proves _run_chat_loop wiring)."""
    from modulatio import comptroller, runners
    from modulatio.orchestration import Orchestrator
    from modulatio.tools import Tool

    calls = []
    registry = {"research_search": Tool(
        name="research_search", description="d",
        call=lambda **kw: calls.append(kw) or "results",
        params_schema={"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
        cost_class="paid-cloud")}
    monkeypatch.setattr(
        comptroller, "authorize_metered_tool",
        lambda *a, **k: comptroller.Authorization(
            allowed=True, refresh_at=None, reason="ok"))

    orch = _make_min_orchestrator(tmp_path, registry)  # see note below
    out = orch._run_chat_loop(
        prompt="p", tool_loadout=("research_search",), role="drafter",
        agent_id="a1", task_id="T-1",
        transcript_path=tmp_path / "t.jsonl", skill_name="s")
    assert calls == [{"query": "modulatio"}]


def test_chat_loop_denies_metered_without_budget(tmp_path, monkeypatch):
    """No budget → the tool result is the DENIED string, loop still returns."""
    ...
```

**Implementer note (verify by reading, not assuming):** the existing suite has orchestrator chat-loop tests — find them with `grep -rln "_run_chat_loop" tests/` and copy that file's fixture pattern for `_make_min_orchestrator` (a scripted chat_runner that first returns a tool_call for `research_search` with `{"query": "modulatio"}`, then a final text). Write BOTH tests fully with that pattern before implementing; the sketch above pins the assertions, the fixture idiom comes from the neighboring test file.

- [ ] **Step 2: Run to verify failure** — the metered call must come back `DENIED (metered): ... no spend authorizer is wired`, failing the `calls ==` assertion.

- [ ] **Step 3: Implement.** In `_run_chat_loop`, after `primary_model = self._resolve_chat_runner_model(agent_id)` and before `def _run_one(...)`, insert:

```python
            # Service-API pool (spec 2026-07-05): metered tools in this
            # loadout get a fail-closed spend authorizer. One authorizer per
            # metered tool (the name guard's contract), dispatched by name.
            # allowed_keys = the tool's own params_schema properties — the
            # schema allowlist metered.py's contract asks for. pinned_units
            # is empty: generation-class calls have no artifact inputs; the
            # idempotency key covers tool + options.
            _registry = self._active_tool_registry()
            _per_tool_auth: dict[str, Callable[[str, dict], tuple]] = {}
            for _name in tool_loadout:
                _tool = _registry.get(_name)
                if getattr(_tool, "cost_class", None) not in (
                    "paid-cloud", "premium-cloud"
                ):
                    continue
                from modulatio import metered as _metered
                from modulatio import services as _services
                _svc_cap = 1
                _svc = None
                for _s in _services.load_services().values():
                    if _name == "api_call" or any(
                        _name.endswith(_c) or _c in _name
                        for _c in _s.capabilities
                    ):
                        _svc_cap = max(_svc_cap, _s.per_task_cap)
                _props = tuple(
                    ((_tool.params_schema or {}).get("properties") or {})
                    .keys()
                )
                _per_tool_auth[_name] = _metered.build_metered_authorizer(
                    project_code=self.project.code,
                    cost_class=_tool.cost_class,
                    tool_name=_name,
                    task_id=task_id,
                    agent_id=agent_id,
                    pinned_units=[],
                    artifacts_root=self._artifacts_root(),
                    per_task_cap=_svc_cap,
                    allowed_keys=_props,
                )
            metered_authorizer = None
            if _per_tool_auth:
                def metered_authorizer(name, args, _m=_per_tool_auth):
                    auth = _m.get(name)
                    if auth is None:
                        return (False, f"metered tool {name!r}: no "
                                       "authorizer wired for it")
                    return auth(name, args)
```

Then thread it into the call inside `_run_one`:

```python
                return _runners.run_llm_with_tools(
                    ...existing kwargs...,
                    metered_authorizer=metered_authorizer,
                    should_abort=self.abort_event.is_set,
                )
```

**Simplification directive:** the `_svc_cap` loop above is the plan's sketch of "per-service per_task_cap"; if it reads muddy in place, simplify to a helper `services.per_task_cap_for_tool(tool_name) -> int` (capability tools → their resolved service's cap; `api_call` → max cap across configured services; default 1) with its own unit test in `tests/test_services.py` — that is the better factoring; prefer it.

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_service_metering.py -x -q`, then the neighbors: `python -m pytest tests/test_llm_with_tools.py -q` → PASS.

- [ ] **Step 5: Ruff + commit**

```bash
ruff check src/modulatio/orchestration.py src/modulatio/services.py tests/
git add -u
git commit -m "Services S7: metered wiring — _run_chat_loop builds per-tool spend authorizers (was dormant-denied)"
```

---

### Task 8: seed skills

**Files:**
- Create: `src/modulatio/_seed_skills/generate-images.md`, `generate-video.md`, `generate-speech.md`, `research-via-api.md`, `service-api-call.md`
- Test: `tests/test_services.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_seed_service_skills_load_and_declare_loadouts():
    from modulatio import skills
    expected = {
        "generate-images": "generate_image",
        "generate-video": "generate_video",
        "generate-speech": "generate_speech",
        "research-via-api": "research_search",
        "service-api-call": "api_call",
    }
    for name, tool in expected.items():
        sk = skills.load_with_metadata(name)
        assert sk is not None, f"seed skill {name} must resolve"
        assert tool in sk.tool_loadout
        assert sk.executor == "llm"
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_services.py -x -q` → FAIL (skills don't resolve). If `load_with_metadata`'s signature differs (check `skills.py:296` area), match the real loader.

- [ ] **Step 3: Write the five seed skills.** Follow the exact frontmatter shape of `_seed_skills/code-assembly.md`. Full text for the two load-bearing ones; the other three follow the same mold (write them completely in the same voice, ~15 lines each):

`generate-images.md`:

```markdown
---
name: generate-images
description: Generate an image from a text prompt via the operator's configured image service (the service-API pool). The image is saved into the artifacts tree; you reference it by filename.
executor: llm
capability_tags: image-generation, media, tool-using
required_capabilities: writing
freshness_class: stable
tool_loadout: generate_image
---

You can generate images with the `generate_image` tool. The operator has
configured an outside image service; the engine checks its API key out of the
pool and injects it — you never see or need the key.

## How to call it

- `prompt` — describe the image precisely: subject, style, composition,
  lighting. One clear paragraph beats keyword soup.
- `size` — e.g. `1024x1024` (default). Only ask for what the task needs.
- `filename` — a descriptive basename ending `.png` (e.g. `cover-art.png`).

The tool SAVES the image into the artifacts tree and returns the filename —
reference the image by that filename in your deliverable; never try to inline
image bytes into text.

## Discipline

- **Metered spend.** Each call may cost the operator real money and is
  budget-gated. Compose the prompt carefully and call ONCE; a denied call
  (`DENIED (metered)`) means the budget is exhausted — report it in your
  summary, don't retry.
- If the tool reports no service/key configured, say so in your summary —
  that's an operator setup step, not something you can fix.
```

`service-api-call.md`:

```markdown
---
name: service-api-call
description: Call any operator-configured outside service's API through the generic api_call tool (relative to the service's pinned base URL). For services without a purpose-built tool.
executor: llm
capability_tags: api-integration, tool-using
required_capabilities: writing
freshness_class: stable
tool_loadout: api_call
---

`api_call` reaches any service the operator configured in the SERVICES pool.
Auth is injected by the engine — you never handle keys.

## How to call it

- `service` — the configured service id (an unknown id returns the list of
  configured ones).
- `path` — RELATIVE to the service's pinned base URL (`v1/things`). Absolute
  URLs are refused by design; you cannot choose the host.
- `method`, `params` (query dict), `json` (body dict) as the API requires.

## Discipline

- Look for a skill named for the service first (the Leader may have authored
  one documenting its endpoints) — `search_skills` before guessing shapes.
- Metered spend: budget-gated per call. Plan the call, make it count, treat
  `DENIED (metered)` as a budget stop to report, not retry.
- An HTTP 4xx/5xx comes back as the tool result — read the body, fix your
  request shape, or report the service-side failure.
```

`research-via-api.md`, `generate-video.md`, `generate-speech.md`: same mold — tool name, param guidance (`query`/`max_results`; `prompt`/`filename`; `text`/`voice`/`filename`), the metered-spend discipline paragraph, and (video) "generation takes minutes; the tool blocks and returns the saved filename or a timeout naming the vendor job id — put the job id in your summary if it times out."

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_services.py -x -q` → PASS.

- [ ] **Step 5: Ruff + commit**

```bash
git add src/modulatio/_seed_skills/ tests/test_services.py
git commit -m "Services S8: seed skills — the five service-capability library entries"
```

---

### Task 9: TUI — the SERVICES section in ConfigScreen

**Files:**
- Modify: `src/modulatio/tui/screens/configuration.py`
- Test: `tests/test_services_screen.py`

**Before writing:** read the whole `ConfigScreen` class and the existing provider key-slot companion, then find its test file (`grep -rln "ConfigScreen" tests/`) and copy that harness idiom (Textual pilot or widget-level, whichever the house uses). Feng-Tui conventions apply — reuse `cfg-section` styling, ConfirmModal for Remove.

- [ ] **Step 1: Write the failing tests** — following the existing config-screen test idiom:

```python
"""SERVICES section of the Config tab."""
# Fixture pattern: copy the existing ConfigScreen test harness. Assertions:


def test_services_section_lists_configured_services(...):
    # add tavily via services.add_service; mount screen; assert the
    # SERVICES table shows "Tavily" with capability "research",
    # key count 0, and "metered".


def test_add_catalog_service_flow(...):
    # trigger Add service → catalog OptionList shows the 4 seeds with
    # (beta) markers; picking one + confirming writes services.json.


def test_add_custom_service_requires_base_url(...):
    # the custom form with empty base_url shows the validation error and
    # does NOT write services.json.


def test_manage_keys_reuses_slot_companion(...):
    # selecting a service and Manage keys opens the key-slot companion
    # bound to the service's env_var (same widget the providers use).


def test_remove_service_confirm_guard(...):
    # Remove asks ConfirmModal; cancel keeps the entry.


def test_operator_strings_escaped(...):
    # a custom service named "[/]boom" renders without MarkupError
    # (the existing escape() guard pattern).
```

Write these fully against the discovered harness — the assertions above are the contract; the mounting/pilot code comes from the neighboring test file.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** In `ConfigScreen.show_list`, after the PROVIDERS & KEYS block, append the SERVICES section:

```python
        # ── SERVICES: the outside-service API pool (spec 2026-07-05) ──
        await lst.mount(Static(
            "SERVICES — outside APIs (image, video, speech, research, "
            "custom); keys pool like provider keys", classes="cfg-section"))
        svc_table = DataTable(id="cfg-services", cursor_type="row")
        svc_table.add_columns("Service", "Capabilities", "Keys", "Tier")
        from modulatio import provider_keys as _pk
        from modulatio import services as _services
        for sid, svc in sorted(_services.load_services().items()):
            n = len([s for s in _pk.list_keys(svc.env_var) if s["is_set"]])
            svc_table.add_row(
                escape(svc.name), escape(", ".join(svc.capabilities)),
                f"{n} key(s)",
                "free" if svc.free_tier else "metered",
                key=sid,
            )
        await lst.mount(svc_table)
        await lst.mount(Horizontal(
            Button("+ Add service", id="cfg-svc-add", variant="primary"),
            Button("Keys", id="cfg-svc-keys"),
            Button("Default", id="cfg-svc-default"),
            Button("Remove", id="cfg-svc-remove", variant="warning"),
            id="cfg-svc-buttons",
        ))
```

Companion flows (each a small widget swapped in via the existing `self._swap(...)`, Cancel already provided):

1. **Add service** — an OptionList: every `service_catalog.catalog()` entry not yet configured, labeled `f"{e.service.name:24} {', '.join(e.service.capabilities)}{'  (beta)' if e.beta else ''}"`, plus a final "Custom service…" option. Picking a catalog entry → confirm → `services.add_service(entry.service)` + prompt for a key (jump straight into the key companion). Picking Custom → a form (`Input` fields: id, name, base URL, auth shape, capabilities CSV, docs URL; `Checkbox` free tier); Save validates via `services.add_service` inside try/except ValueError and paints the error on the form.
2. **Keys** — reuse the provider key-slot companion bound to the selected service's `env_var` (it is provider-agnostic already; if it takes a provider object, lift the env-var-only path the same way the standalone PROVIDERS & KEYS list uses it).
3. **Default** — for a capability with >1 backing service: OptionList of capabilities → OptionList of services → `services.set_capability_default(...)`.
4. **Remove** — ConfirmModal (existing guard pattern) → `services.remove_service(sid)`; note in the modal body that keys stay in the vault `.env` until removed under Keys.

Budget note: the table's Tier column + doctor point at the comptroller budget; there is no Set-budget companion in this task (see Task 10 — CLI/doctor own the budget surface; a TUI budget editor is deferred with the spec's blessing if `comptroller.md` editing proves awkward in the screen — record whichever way it lands in the CHANGELOG).

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_services_screen.py -x -q`, then the whole TUI suite slice: `python -m pytest tests/ -q -k "screen or tui or config"`.

- [ ] **Step 5: Ruff + commit**

```bash
ruff check src/modulatio/tui/screens/configuration.py tests/test_services_screen.py
git add -u && git add tests/test_services_screen.py
git commit -m "Services S9: the SERVICES section — catalog/custom add, key pool, defaults, guarded remove"
```

---

### Task 10: budget writer + doctor checks

**Files:**
- Modify: `src/modulatio/comptroller.py` (add `set_budget_field`)
- Modify: `src/modulatio/services.py` (add `doctor_report`)
- Modify: `src/modulatio/cli.py` (`_run_doctor_checks`, ~line 858)
- Tests: `tests/test_service_metering.py`, `tests/test_services.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_service_metering.py
def test_set_budget_field_round_trips(tmp_path, monkeypatch):
    from modulatio import comptroller
    # monkeypatch comptroller's project config path resolution the same way
    # the existing comptroller tests do (copy their fixture), then:
    comptroller.set_budget_field("SVC", "paid_cloud_escalations_per_day", 5)
    assert comptroller.load_budget("SVC").paid_cloud_per_day == 5
    comptroller.set_budget_field("SVC", "paid_cloud_escalations_per_day", 9)
    assert comptroller.load_budget("SVC").paid_cloud_per_day == 9


# tests/test_services.py
def test_doctor_report_flags_key_without_budget(monkeypatch):
    services.add_service(_svc())          # metered, no budget
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-x")
    lines = services.doctor_report("SVC")
    assert any("no paid-cloud budget" in ln for ln in lines)


def test_doctor_report_flags_service_without_key(monkeypatch):
    services.add_service(_svc())
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    lines = services.doctor_report("SVC")
    assert any("no API key" in ln for ln in lines)


def test_doctor_report_quiet_when_healthy(monkeypatch):
    services.add_service(_svc(free_tier=True))
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-x")
    assert services.doctor_report("SVC") == []
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.**

`comptroller.set_budget_field` (reuse `_config_path` + `_OWN_FRONTMATTER_RE`; create the file with a minimal frontmatter block if missing, else rewrite/insert the one `key: value` line inside the existing frontmatter, body untouched):

```python
def set_budget_field(project_code: str, field: str, value: int) -> None:
    """Write ONE budget frontmatter field into the project's comptroller
    config (the SERVICES tab / doctor's budget surface). Creates the file
    with a bare frontmatter block when missing."""
    path = _config_path(project_code)
    line = f"{field}: {int(value)}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{line}\n---\n", encoding="utf-8")
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    m = _OWN_FRONTMATTER_RE.match(text)
    if not m:
        path.write_text(f"---\n{line}\n---\n{text}", encoding="utf-8")
        return
    lines = m.group(1).splitlines()
    lines = [ln for ln in lines
             if ln.partition(":")[0].strip() != field] + [line]
    new_front = "\n".join(lines)
    path.write_text(
        text[:m.start(1)] + new_front + text[m.end(1):], encoding="utf-8"
    )
```

`services.doctor_report`:

```python
def doctor_report(project_code: str) -> list[str]:
    """Doctor lines for the service pool — empty list = healthy. Flags the
    three spec conditions: metered-but-unbudgeted, keyless, and a custom
    entry with a broken base_url (load_services already drops those, so
    surface raw entries)."""
    from modulatio import comptroller
    lines: list[str] = []
    svcs = load_services()
    raw = _load_raw().get("services", {})
    for sid in raw:
        if sid not in svcs:
            lines.append(f"service {sid!r}: entry invalid/corrupt — re-add it")
    if not svcs:
        return lines
    budget = comptroller.load_budget(project_code)
    for svc in svcs.values():
        if checkout_key(svc) is None:
            lines.append(
                f"service {svc.id!r}: no API key in any {svc.env_var} slot"
            )
        if not svc.free_tier and budget.paid_cloud_per_day is None:
            lines.append(
                f"service {svc.id!r}: metered but no paid-cloud budget for "
                f"project {project_code!r} — every call will be denied "
                "(set paid_cloud_escalations_per_day)"
            )
    return lines
```

In `cli.py` `_run_doctor_checks`, add a services block following the neighboring checks' exact print/OK-WARN idiom (read two adjacent checks first), reporting each `services.doctor_report(<default project code>)` line as a WARN and "services: OK (N configured)" when the list is empty and any service exists.

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_service_metering.py tests/test_services.py -q`, then run the real thing and LOOK at it: `python -m modulatio.cli doctor` (or the installed `modulatio doctor`) — the services block must render.

- [ ] **Step 5: Ruff + commit**

```bash
ruff check src/ tests/
git add -u
git commit -m "Services S10: budget writer + doctor — unbudgeted/keyless services surface before they bite"
```

---

### Task 11: redaction + sandbox verification (observed reality, not assumption)

**Files:**
- Test: `tests/test_service_tools.py` (append)
- Possibly modify: `src/modulatio/logstore.py` (only if the check fails)

The spec requires: log auto-redaction and run_shell env-scrub cover service keys. VERIFY, don't assume:

- [ ] **Step 1:** Read the redaction implementation (`grep -n "redact" src/modulatio/logstore.py`) and the sandbox env handling (`grep -n "env" src/modulatio/sandbox.py | head -20`). Determine: does redaction key on `*_API_KEY` env values? Does the sandbox scrub non-passthrough env?

- [ ] **Step 2:** Write the test that pins the truth:

```python
def test_service_key_redacted_in_logstore(monkeypatch):
    from modulatio import logstore
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-secret-value")
    # Use logstore's real redaction entrypoint (found in step 1):
    redacted = logstore.redact("calling with sk-test-secret-value now")
    assert "sk-test-secret-value" not in redacted
```

(Adjust to the real entrypoint name found in Step 1.)

- [ ] **Step 3:** If it fails, extend the redaction sweep to include values of every `provider_keys.pool_env_vars(svc.env_var)` slot for configured services — smallest change inside the existing redaction function. If it passes, keep the test as the regression pin.

- [ ] **Step 4:** `python -m pytest tests/test_service_tools.py -q` → PASS.

- [ ] **Step 5: Commit** — `git add -u && git commit -m "Services S11: redaction pin — service keys never reach logs"`

---

### Task 12: durable docs + CHANGELOG (all the fixin's)

**Files:**
- Modify: `src/modulatio/_docs/26-tools.md` (service tools + the now-live metered wiring)
- Modify: `CHANGELOG.md`
- Check for staleness: `README.md` feature list, `docs/` site catalogs that enumerate tools/config sections (grep for "web_search" and "PROVIDERS" across docs to find every durable surface that enumerates tools/config)

- [ ] **Step 1:** Update `_docs/26-tools.md`: the five service tools (name, params, metered default, binary-to-artifact contract), the SERVICES pool (checkout order, pinned base URL rule), and correct the metered section — the authorizer is now WIRED in `_run_chat_loop` (it previously described an unwired contract).
- [ ] **Step 2:** CHANGELOG entry under the next version heading, following the house voice.
- [ ] **Step 3:** Grep the durable docs for every surface enumerating tools or Config sections; update each. List what you touched in the commit body.
- [ ] **Step 4: Commit** — `git commit -m "Services S12: durable docs — tools catalog, SERVICES section, CHANGELOG"`

---

### Task 13: full gates + live smoke

- [ ] **Step 1:** `ruff check src/ tests/` → clean.
- [ ] **Step 2:** `python -m pytest tests/ -q` → full suite green (do NOT pipe through `tail` — it masks the exit code; the known load-flake `test_cancel_not_clobbered_by_concurrent_update` re-runs solo if it trips).
- [ ] **Step 3:** Live smoke, observed reality: launch the TUI (`modulatio`), open Config → SERVICES, add Tavily from the catalog with a FAKE key, confirm the table row + doctor WARN (no budget), remove it. No crash, escapes hold.
- [ ] **Step 4:** Final commit of any smoke fallout; report status honestly (green/red, what was observed).

---

## Deferred (named so they stay dead until earned)

- Key-slot rotation **on vendor error** (v1 = first-set-slot; adapters fail with clear errors).
- A TUI budget-editor companion if Task 9 lands it doctor-side only.
- OAuth-flow services; more catalog vendors; a `document` capability tool.
- Leader-authored custom-service skills need NO new code (existing skill-create lane) — but a live rehearsal belongs in the post-ship crack campaign.

## Self-review notes (already applied)

- Spec coverage: storage/pool (T1), catalog (T2), metering (T3, T7), tools + binary rails + async (T4-T6), skills (T8), TUI (T9), doctor + budget (T10), redaction (T11), docs (T12). Leader superset needs no task — same registry, gate already in place (spec §8).
- Type consistency: `Service` fields used identically in T1/T2/T4-T6/T9/T10; `build_service_tools(artifacts_root, on_artifact_write)` matches the `build_registry` params it's called with (T6); `allowed_keys: tuple[str, ...]` matches T3's signature at T7's call site.
- Known verify-at-build points are marked inline (provider_keys constant names, `_cap_http_body` signature, chat-loop test harness idiom, skills loader name, doctor print idiom) — each says exactly what to read first; none is a design placeholder.
