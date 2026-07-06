"""Shipped service catalog — vendor entries the SERVICES tab offers."""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import provider_keys, service_catalog, services
from modulatio.services import Service


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(services, "SERVICES_FILE", tmp_path / "services.json")
    monkeypatch.setattr(
        provider_keys, "LABELS_FILE", tmp_path / "key_labels.json"
    )
    monkeypatch.setattr(provider_keys, "PINS_FILE", tmp_path / "key_pins.json")


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


def test_catalog_entries_pass_add_service_validation():
    for e in service_catalog.catalog():
        services.add_service(e.service)
        assert services.load_services()[e.service.id].name == e.service.name
