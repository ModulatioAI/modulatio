# SPDX-License-Identifier: Apache-2.0
"""Tests for the SERVICES section of the Configuration tab (service-API pool,
spec 2026-07-05). Follows the ``tests/test_tui/test_configuration.py`` harness
idiom: a Textual pilot host mounting ConfigScreen, driving handlers directly
where pilot geometry would be flaky, asserting on mounted widgets + the real
``services.json`` state (the global conftest isolates SERVICES_FILE per test).
"""
from __future__ import annotations

from rich.errors import MarkupError as RichMarkupError
from textual.app import App, ComposeResult
from textual.widgets import Button, DataTable, Input, OptionList, Static
from textual.widgets._data_table import default_cell_formatter

from modulatio import service_catalog, services
from modulatio.services import Service
from modulatio.tui.screens.configuration import (
    ConfigScreen,
    _multi_backed_capabilities,
)


class _Host(App):
    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        v.setdefault("frame-dim", "#3f6d8c")
        return v

    def compose(self) -> ComposeResult:
        yield ConfigScreen(id="cfg")


def _svc(**over) -> Service:
    base = dict(
        id="tavily", name="Tavily", kind="catalog",
        capabilities=("research",), env_var="TAVILY_API_KEY",
        base_url="https://api.tavily.com", auth_shape="bearer",
        free_tier=False, docs_url="", per_task_cap=1,
    )
    base.update(over)
    return Service(**base)


async def _press(app: App, screen: ConfigScreen, button_id: str) -> None:
    """Drive a list-pane button through the screen's own handler — the list
    pane can be squeezed below click geometry in a test terminal, so the
    Pressed-event idiom (existing config tests) beats pilot.click here."""
    await screen.on_button_pressed(
        Button.Pressed(app.query_one(f"#{button_id}", Button)))


# ── the services table ────────────────────────────────────────────────────

async def test_services_table_lists_configured_services(monkeypatch):
    services.add_service(_svc())  # metered, research
    services.add_service(_svc(
        id="freebie", name="Freebie", kind="custom", capabilities=("image",),
        env_var="FREEBIE_API_KEY", base_url="https://free.example",
        free_tier=True))
    monkeypatch.setenv("TAVILY_API_KEY", "k1")
    monkeypatch.delenv("FREEBIE_API_KEY", raising=False)
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        table = app.query_one("#cfg-services", DataTable)
        assert table.row_count == 2
        rows = {tuple(table.get_row_at(i)) for i in range(2)}
        assert ("Tavily", "research", "1 key(s)", "metered") in rows
        assert ("Freebie", "image", "0 key(s)", "free") in rows


# ── add service: the catalog lane ─────────────────────────────────────────

async def test_add_service_catalog_excludes_configured_and_marks_beta():
    services.add_service(service_catalog.entry("tavily").service)
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await _press(app, screen, "cfg-svc-add")
        await pilot.pause()
        ol = app.query_one("#cfg-svc-catalog", OptionList)
        opts = [ol.get_option_at_index(i) for i in range(ol.option_count)]
        ids = [o.id for o in opts]
        assert "tavily" not in ids          # already configured → excluded
        assert "openai-images" in ids       # unconfigured entries offered
        assert ids[-1] == "__custom__"      # the custom lane closes the list
        labels = [str(o.prompt) for o in opts]
        assert any("(beta)" in label for label in labels)


async def test_catalog_pick_adds_service_and_jumps_to_its_keys():
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await screen._add_catalog_service("tavily")
        await pilot.pause()
        svc = services.get_service("tavily")
        assert svc is not None and svc.env_var == "TAVILY_API_KEY"
        # …and the operator landed straight in the key companion, bound to
        # the SERVICE's env var (the reused provider key-slot manager).
        assert app.query("#cfg-provkeylist")
        assert screen._prov_base == "TAVILY_API_KEY"
        assert screen._prov_id == "svc:tavily"


# ── add service: the custom lane ──────────────────────────────────────────

def _fill_custom(app, *, name="My API", url="https://api.mine.dev",
                 authkind="header", authname="X-Key", capkind="__custom__",
                 capcustom="image, speech", docs="https://docs.mine.dev",
                 free=True):
    from textual.widgets import Checkbox, Select
    app.query_one("#cfg-csvc-name", Input).value = name
    app.query_one("#cfg-csvc-url", Input).value = url
    app.query_one("#cfg-csvc-authkind", Select).value = authkind
    app.query_one("#cfg-csvc-authname", Input).value = authname
    app.query_one("#cfg-csvc-capkind", Select).value = capkind
    app.query_one("#cfg-csvc-capcustom", Input).value = capcustom
    app.query_one("#cfg-csvc-docs", Input).value = docs
    app.query_one("#cfg-csvc-free", Checkbox).value = free


async def test_custom_form_shows_the_two_step_header():
    """#2: the form tells the user up front it's create-then-add-key."""
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await screen._show_custom_service_form()
        await pilot.pause()
        head = " ".join(str(s.content) for s in app.query(Static))
        assert "add its API key" in head and "select" in head.lower()


async def test_custom_form_empty_base_url_paints_error_and_persists_nothing():
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await screen._show_custom_service_form()
        await pilot.pause()
        _fill_custom(app, url="")
        await _press(app, screen, "cfg-csvc-save")
        await pilot.pause()
        assert services.load_services() == {}          # nothing persisted
        assert app.query("#cfg-csvc-name")             # the form survives
        status = str(app.query_one("#cfg-status", Static).content)
        assert "base_url" in status                    # rejection painted


async def test_custom_form_happy_path_persists_the_entered_fields():
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await screen._show_custom_service_form()
        await pilot.pause()
        _fill_custom(app)
        await _press(app, screen, "cfg-csvc-save")
        await pilot.pause()
        # #4: id is DERIVED from the name (one human field), not typed.
        svc = services.get_service("my-api")
        assert svc is not None
        assert svc.name == "My API"
        assert svc.kind == "custom"
        assert svc.base_url == "https://api.mine.dev"
        assert svc.auth_shape == "header:X-Key"        # kind + name composed
        assert svc.capabilities == ("image", "speech")
        assert svc.docs_url == "https://docs.mine.dev"
        assert svc.free_tier is True
        # #4: key handle is OPAQUE — no service name on file, ever.
        assert svc.env_var.startswith("SVCKEY_")
        assert "MY" not in svc.env_var and "API_KEY" not in svc.env_var
        assert app.query_one("#cfg-services", DataTable).row_count == 1


async def test_custom_form_bearer_ignores_the_name_field():
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await screen._show_custom_service_form()
        await pilot.pause()
        _fill_custom(app, name="Plain", authkind="bearer", authname="ignored")
        await _press(app, screen, "cfg-csvc-save")
        await pilot.pause()
        assert services.get_service("plain").auth_shape == "bearer"


async def test_custom_form_capability_from_dropdown():
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await screen._show_custom_service_form()
        await pilot.pause()
        _fill_custom(app, name="Rsch", capkind="research", capcustom="")
        await _press(app, screen, "cfg-csvc-save")
        await pilot.pause()
        assert services.get_service("rsch").capabilities == ("research",)


async def test_custom_form_capability_dropdown_has_custom_on_top():
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await screen._show_custom_service_form()
        await pilot.pause()
        from textual.widgets import Select
        sel = app.query_one("#cfg-csvc-capkind", Select)
        values = [v for _, v in sel._options]  # (label, value) pairs
        assert values[0] == "__custom__"
        assert len(values) >= 14  # custom + 13


# ── keys: the reused provider key-slot companion ──────────────────────────

async def test_keys_button_opens_the_key_manager_on_the_service_env_var(
    monkeypatch,
):
    services.add_service(_svc())
    monkeypatch.setenv("TAVILY_API_KEY", "k1")
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        app.query_one("#cfg-services", DataTable).move_cursor(row=0)
        await pilot.pause()
        await _press(app, screen, "cfg-svc-keys")
        await pilot.pause()
        keylist = app.query_one("#cfg-provkeylist", OptionList)
        assert keylist.option_count == 1   # the one set TAVILY key slot
        assert keylist.get_option_at_index(0).id == "TAVILY_API_KEY"
        assert screen._prov_base == "TAVILY_API_KEY"


# ── defaults ──────────────────────────────────────────────────────────────

def test_multi_backed_capabilities_enumeration():
    services.add_service(_svc())
    services.add_service(_svc(id="other", name="Other",
                              env_var="OTHER_API_KEY"))
    services.add_service(_svc(id="img", name="Img", capabilities=("image",),
                              env_var="IMG_API_KEY"))
    multi = _multi_backed_capabilities()
    assert list(multi) == ["research"]            # image has ONE backer
    assert [s.id for s in multi["research"]] == ["other", "tavily"]


async def test_default_flow_sets_the_capability_default():
    services.add_service(_svc())
    services.add_service(_svc(id="other", name="Other",
                              env_var="OTHER_API_KEY"))
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await _press(app, screen, "cfg-svc-default")
        await pilot.pause()
        caps = app.query_one("#cfg-svc-caplist", OptionList)
        assert [caps.get_option_at_index(i).id
                for i in range(caps.option_count)] == ["research"]
        await screen._show_default_picker("research")
        await pilot.pause()
        assert app.query("#cfg-svc-deflist")
        await screen._set_service_default("other")
        await pilot.pause()
        assert services.capability_default("research") == "other"
        assert app.query("#cfg-services")  # back on the refreshed list


async def test_default_with_no_multi_backed_capability_shows_a_status():
    services.add_service(_svc())  # a single backer — nothing to choose
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await _press(app, screen, "cfg-svc-default")
        await pilot.pause()
        assert not app.query("#cfg-svc-caplist")
        status = str(app.query_one("#cfg-list-status", Static).content)
        assert "more than one backing service" in status


# ── remove (guarded) ──────────────────────────────────────────────────────

async def test_remove_service_cancel_keeps_then_confirm_removes():
    services.add_service(_svc())
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        app.query_one("#cfg-services", DataTable).move_cursor(row=0)
        await pilot.pause()
        # cancel → the service stays
        await _press(app, screen, "cfg-svc-remove")
        await pilot.pause()
        await pilot.click("#confirm-no")
        await pilot.pause()
        assert services.get_service("tavily") is not None
        assert app.query_one("#cfg-services", DataTable).row_count == 1
        # confirm → the service goes (keys untouched by design)
        await _press(app, screen, "cfg-svc-remove")
        await pilot.pause()
        await pilot.click("#confirm-yes")
        await pilot.pause()
        assert services.get_service("tavily") is None
        assert app.query_one("#cfg-services", DataTable).row_count == 0


# ── the MarkupError escape guard (preship idiom — deterministic paint) ────

class _CapturingTable:
    """Stand-in for DataTable recording the cells ``add_row`` is given."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def add_row(self, *cells, key=None):
        self.rows.append(cells)


def test_services_table_escapes_a_markup_bearing_custom_name():
    services.add_service(_svc(
        id="boom", name="[/]boom", kind="custom",
        capabilities=("image[/x]",), env_var="BOOM_API_KEY",
        base_url="https://boom.example"))
    table = _CapturingTable()
    ConfigScreen._fill_services_table(object.__new__(ConfigScreen), table)
    assert len(table.rows) == 1
    for cell in table.rows[0]:
        try:
            rendered = default_cell_formatter(cell)
        except RichMarkupError:
            raise AssertionError(f"cell {cell!r} crashed the table paint")
        assert rendered is not None
    # the literal brackets survive (escaped, not parsed away as markup)
    assert "[/]boom" in str(default_cell_formatter(table.rows[0][0]))


async def test_custom_service_key_companion_hides_the_opaque_handle():
    """#4: the operator sees the service NAME, never the SVCKEY_ handle."""
    services.add_service(Service(
        id="ocrspace", name="OCR.space", kind="custom", capabilities=("ocr",),
        env_var="SVCKEY_ABC123", base_url="https://api.ocr.space",
        auth_shape="header:apikey"))
    app = _Host()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await screen._show_provider_keys("svc:ocrspace")
        await pilot.pause()
        head = " ".join(str(s.content) for s in app.query(Static))
        assert "OCR.space" in head          # the note IS shown
        assert "SVCKEY_" not in head         # the opaque handle is NOT
