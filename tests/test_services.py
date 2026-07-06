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


def test_remove_service_drops_its_capability_default():
    services.add_service(_svc())
    services.add_service(_svc(id="other", name="Other",
                              env_var="OTHER_API_KEY"))
    services.set_capability_default("research", "other")
    assert services.remove_service("other") is True
    assert services.capability_default("research") is None
    # the survivor is the only backer again → resolution recovers
    assert services.resolve_for_capability("research").id == "tavily"


def test_set_capability_default_rejects_non_backing_service():
    services.add_service(_svc())  # backs research only
    with pytest.raises(ValueError):
        services.set_capability_default("image", "tavily")
    with pytest.raises(ValueError):
        services.set_capability_default("research", "never-added")
    assert services.capability_default("image") is None


def test_capability_default_round_trip_and_clear():
    services.add_service(_svc())
    assert services.capability_default("research") is None
    services.set_capability_default("research", "tavily")
    assert services.capability_default("research") == "tavily"
    services.clear_capability_default("research")
    assert services.capability_default("research") is None
    services.clear_capability_default("research")  # idempotent — no raise


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


# ── S10: doctor_report — pool health surfaced before a run hits it ──────────

DOCTOR_CODE = "SVD"


@pytest.fixture
def _project(tmp_path: Path, monkeypatch) -> None:
    from modulatio import vault
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    vault.init_project(DOCTOR_CODE, "svc doctor", "doctor report tests")


def test_doctor_report_flags_metered_without_budget(_project, monkeypatch):
    services.add_service(_svc())  # free_tier=False → metered
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test")
    lines = services.doctor_report(DOCTOR_CODE)
    assert any("no paid-cloud budget" in ln for ln in lines)
    assert not any("no API key" in ln for ln in lines)


def test_doctor_report_flags_missing_key(_project, monkeypatch):
    from modulatio import comptroller
    services.add_service(_svc())
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    comptroller.set_budget_field(
        DOCTOR_CODE, "paid_cloud_escalations_per_day", 5
    )
    lines = services.doctor_report(DOCTOR_CODE)
    assert any("no API key" in ln for ln in lines)
    assert not any("no paid-cloud budget" in ln for ln in lines)


def test_doctor_report_healthy_is_empty(_project, monkeypatch):
    services.add_service(_svc(free_tier=True))
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test")
    assert services.doctor_report(DOCTOR_CODE) == []


def test_doctor_report_flags_corrupt_raw_entry(_project, monkeypatch):
    services.add_service(_svc(free_tier=True))
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test")
    data = json.loads(services.SERVICES_FILE.read_text(encoding="utf-8"))
    data["services"]["bogus"] = "not-a-dict"
    services.SERVICES_FILE.write_text(json.dumps(data), encoding="utf-8")
    lines = services.doctor_report(DOCTOR_CODE)
    assert any("invalid/corrupt" in ln and "bogus" in ln for ln in lines)
    assert len(lines) == 1  # the healthy service adds no noise


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


# ── custom-service opaque key handle (form UX #4) ──────────────────────────

def test_new_key_handle_is_opaque_and_prefixed():
    """A custom service's key handle carries NO service name/slug — the vault
    .env must never read a service-named key. Generic SVCKEY_<hex>."""
    h = services.new_key_handle()
    assert h.startswith("SVCKEY_")
    assert h.isidentifier()  # usable as an env var name
    assert "OCR" not in h and "API_KEY" not in h


def test_new_key_handle_avoids_collision_with_existing():
    services.add_service(_svc(
        id="a", name="A", kind="custom", env_var="SVCKEY_AAAAAA",
        base_url="https://a.example"))
    # 200 draws must never reuse the taken handle.
    for _ in range(200):
        assert services.new_key_handle() != "SVCKEY_AAAAAA"
