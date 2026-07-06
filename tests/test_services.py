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


def test_add_rejects_empty_auth_shape_name():
    with pytest.raises(ValueError):
        services.add_service(_svc(auth_shape="header:"))


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


def test_per_task_cap_for_capability_tool():
    services.add_service(_svc(per_task_cap=3))
    assert services.per_task_cap_for_tool("research_search") == 3


def test_per_task_cap_for_api_call_is_max_across_services():
    services.add_service(_svc(per_task_cap=3))
    services.add_service(_svc(id="luma", name="Luma",
                              capabilities=("video",),
                              env_var="LUMA_API_KEY", per_task_cap=5))
    assert services.per_task_cap_for_tool("api_call") == 5


def test_per_task_cap_unknown_or_unresolvable_is_one():
    assert services.per_task_cap_for_tool("mystery_tool") == 1
    # No service backs "image" → tight floor.
    assert services.per_task_cap_for_tool("generate_image") == 1


def test_one_corrupt_entry_does_not_hide_the_rest():
    services.add_service(_svc())
    data = json.loads(services.SERVICES_FILE.read_text(encoding="utf-8"))
    data["services"]["junk"] = "garbage"  # non-dict entry
    services.SERVICES_FILE.write_text(json.dumps(data), encoding="utf-8")
    assert list(services.load_services()) == ["tavily"]
