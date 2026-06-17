#!/usr/bin/env python3
"""Modulatio CONFIG › MODELS tab mockup — Feng-Tui aesthetic.

Mirrors the surface of the real ConfigScreen (configuration.py), re-skinned in the
phosphor/feng-shui terminal style:

    • the configured-model REGISTRY  (Key · Model · Auth · Status)   ← the doorway
    • PROVIDERS & KEYS               (add / remove API keys per provider)
    • + Add model                    (Provider → Auth → Model wizard)
    • Remove                         (drop a configured model)
    • Pin key                        (pin a key to a model to isolate spend)

Left column = the registry (focal). Right pane (40%, full-height divider) hosts the
secondary surfaces — PROVIDERS & KEYS by default; the add-model wizard, a provider's
key manager, and the pin manager swap in.

Keys: ↑↓ move · a add model · r remove · p pin key · k keys pane ·
      enter choose/assign/drill · / filter (models) · esc back · F2 theme · ^Q quit
Static fake data — a look+behaviour mockup, not wired to the engine.
"""
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static, Input, DataTable, OptionList
from textual.widgets.option_list import Option
from textual.binding import Binding
from rich.text import Text

THEMES = [
    {"name": "amber", "accent": "#FFC933", "dim": "#FFB300"},
    {"name": "green", "accent": "#7DFF9C", "dim": "#44FF77"},
    {"name": "cyan",  "accent": "#80EEFF", "dim": "#44E8FF"},
]

# ── auth vocabulary (mirrors provider_catalog.AuthOption) ────────────────
AUTH_LABEL = {
    "api_key": "API key",
    "oauth_anthropic": "Sign in with Claude (OAuth)",
    "oauth_openai": "Sign in (ChatGPT / Codex OAuth)",
    "oauth_xai": "Sign in with Grok (OAuth)",
    "none": "No auth — local server",
}
AUTH_SHORT = {"api_key": "key", "none": "local",
              "oauth_anthropic": "oauth", "oauth_openai": "oauth", "oauth_xai": "oauth"}
OAUTH_HINT = {
    "oauth_anthropic": "run `claude login`",
    "oauth_openai": "run `codex login`",
    "oauth_xai": "install + log in with the Grok CLI (x.ai/cli)",
}
ENV_VAR = {
    "openrouter": "OPENROUTER_API_KEY", "ollama-cloud": "OLLAMA_API_KEY",
    "xai": "XAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY", "nvidia": "NVIDIA_API_KEY",
    "google": "GEMINI_API_KEY", "custom": "CUSTOM_API_KEY",
}


def _prov(pid, name, auth, free, note):
    return dict(id=pid, name=name, auth=auth, free=free, note=note)


# 8 providers + custom — mirrors provider_catalog.PROVIDERS.
PROVIDERS = [
    _prov("openrouter", "OpenRouter", ["api_key"], "~25 free",
          "One key, 340+ models across vendors; ~25 free (rate-limited)."),
    _prov("ollama-cloud", "Ollama Cloud", ["api_key"], "all free*",
          "Hosted Ollama models, OpenAI-compatible; free tier is rate-limited."),
    _prov("xai", "xAI", ["api_key", "oauth_xai"], "—",
          "Grok text · image · video. No free tier."),
    _prov("anthropic", "Anthropic", ["oauth_anthropic", "api_key"], "—",
          "Claude models (text). OAuth or API key. No free tier."),
    _prov("openai", "OpenAI", ["oauth_openai", "api_key"], "—",
          "GPT models (text). ChatGPT/Codex OAuth or API key. No free tier."),
    _prov("nvidia", "NVIDIA", ["api_key"], "all free*",
          "NVIDIA-hosted open models (OpenAI-compatible). Free tier, 40 RPM."),
    _prov("google", "Google (Gemini)", ["api_key"], "all free*",
          "Gemini via the OpenAI-compatible endpoint. Free AI Studio tier."),
    _prov("local", "ollama (local)", ["none"], "all free",
          "Local ollama server (OpenAI-compatible). Lists loaded models."),
    _prov("custom", "Custom / Other", ["api_key"], "—",
          "Any OpenAI-compatible endpoint — paste base URL + key."),
]
PROV_BY_ID = {p["id"]: p for p in PROVIDERS}


def _m(mid, ctx, free, modality="text", pinned=False):
    return dict(id=mid, ctx=ctx, free=free, modality=modality, pinned=pinned)


# Per-provider catalogs for the add-model wizard's step 3. OpenRouter is
# deliberately long to prove the list scales (search + scroll).
CATALOG = {
    "openrouter": [
        _m("openrouter/auto", 0, False, "text", True),
        _m("openrouter/free", 0, True, "text", True),
        _m("anthropic/claude-opus-4.8", 200, False),
        _m("anthropic/claude-sonnet-4.6", 200, False),
        _m("openai/gpt-5.5", 256, False),
        _m("openai/gpt-5.5-mini", 256, False),
        _m("openai/gpt-5.5-nano", 256, True),
        _m("google/gemini-2.5-pro", 1000, False),
        _m("google/gemini-2.5-flash", 1000, True),
        _m("google/gemini-2.0-flash", 1000, True),
        _m("meta-llama/llama-3.3-70b-instruct", 128, True),
        _m("meta-llama/llama-3.1-405b-instruct", 128, False),
        _m("meta-llama/llama-3.1-8b-instruct", 128, True),
        _m("deepseek/deepseek-r1", 128, True),
        _m("deepseek/deepseek-v3", 128, True),
        _m("deepseek/deepseek-chat", 64, True),
        _m("qwen/qwen3-235b-a22b", 256, False),
        _m("qwen/qwen3-coder", 256, True),
        _m("qwen/qwen-2.5-72b-instruct", 128, True),
        _m("mistralai/mistral-large-2411", 128, False),
        _m("mistralai/mixtral-8x22b-instruct", 64, True),
        _m("mistralai/ministral-8b", 128, True),
        _m("x-ai/grok-4", 256, False),
        _m("x-ai/grok-4-mini", 131, False),
        _m("nvidia/nemotron-4-340b-instruct", 4, False),
        _m("microsoft/phi-4", 16, True),
        _m("microsoft/wizardlm-2-8x22b", 64, False),
        _m("cohere/command-r-plus", 128, False),
        _m("cohere/command-r", 128, True),
        _m("01-ai/yi-large", 32, False),
        _m("nousresearch/hermes-3-llama-3.1-405b", 128, True),
        _m("gryphe/mythomax-l2-13b", 4, True),
        _m("perplexity/sonar-pro", 200, False),
        _m("amazon/nova-pro-v1", 300, False),
        _m("liquid/lfm-40b", 32, True),
    ],
    "ollama-cloud": [
        _m("glm-4.6", 128, True), _m("qwen3-coder", 256, True),
        _m("llama-3.3-70b", 128, True), _m("deepseek-r1-70b", 128, True),
        _m("gpt-oss-120b", 128, True), _m("mistral-large", 128, True),
        _m("qwen3-235b", 256, True), _m("gemma3-27b", 128, True),
        _m("phi-4", 16, True), _m("command-r-plus", 128, True),
    ],
    "xai": [
        _m("grok-4", 256, False), _m("grok-4-mini", 131, False),
        _m("grok-3", 131, False), _m("grok-imagine", 0, False, "image"),
        _m("grok-video", 0, False, "video"),
    ],
    "anthropic": [
        _m("claude-opus-4-8", 200, False), _m("claude-sonnet-4-6", 200, False),
        _m("claude-haiku-4-5", 200, False),
    ],
    "openai": [
        _m("gpt-5.5", 256, False), _m("gpt-5.5-mini", 256, False),
        _m("gpt-5.5-nano", 256, False), _m("o4", 200, False),
    ],
    "nvidia": [
        _m("nvidia/llama-3.3-nemotron-70b", 128, True),
        _m("meta/llama-3.1-405b-instruct", 128, True),
        _m("deepseek-ai/deepseek-r1", 128, True),
        _m("qwen/qwen3-coder-480b", 256, True),
        _m("google/gemma-3-27b-it", 128, True),
        _m("mistralai/mixtral-8x22b", 64, True),
        _m("microsoft/phi-4", 16, True), _m("nvidia/nemotron-4-340b", 4, True),
    ],
    "google": [
        _m("gemini-2.5-pro", 1000, True), _m("gemini-2.5-flash", 1000, True),
        _m("gemini-2.5-flash-lite", 1000, True), _m("gemini-2.0-flash", 1000, True),
        _m("gemini-2.0-flash-thinking", 1000, True),
    ],
    "local": [
        _m("glm-4.6", 128, True), _m("qwen3-coder", 256, True),
        _m("llama-3.3", 128, True),
    ],
    "custom": [_m("(enter a model id from your endpoint)", 0, False)],
}
TRUE_COUNT = {"openrouter": 340}


# ── the configured-model registry (mirrors model_presets) ────────────────
def _reg(key, model, prov, auth, ready):
    return dict(key=key, model=model, prov=prov, auth=auth, ready=ready)


REGISTRY_SEED = [
    _reg("opus", "claude-opus-4-8", "anthropic", "oauth_anthropic", True),
    _reg("sonnet", "claude-sonnet-4-6", "anthropic", "oauth_anthropic", True),
    _reg("glm", "glm-4.6", "local", "none", True),
    _reg("qwen-coder", "qwen3-coder", "local", "none", True),
    _reg("or-free", "openrouter/free", "openrouter", "api_key", True),
    _reg("gpt55", "gpt-5.5", "openai", "api_key", False),
    _reg("grok", "grok-4", "xai", "api_key", False),
]

# ── provider key pool (mirrors provider_keys; labels only, never values) ──
def _slot(index, label, is_set, pinned):
    return dict(index=index, label=label, is_set=is_set, pinned=list(pinned))


KEYPOOL = {
    "openrouter": [_slot(1, "main", True, []), _slot(2, "backup", True, ["or-free"])],
    "anthropic": [_slot(1, "", True, [])],
    "ollama-cloud": [], "openai": [], "xai": [],
    "nvidia": [], "google": [], "custom": [],
}


def _api_key_providers():
    return [p for p in PROVIDERS if "api_key" in p["auth"]]


def _n_keys(pid):
    return sum(1 for s in KEYPOOL.get(pid, []) if s["is_set"])


def _slug(model_id):
    tail = model_id.split("/")[-1]
    return "".join(c if c.isalnum() else "-" for c in tail).strip("-").lower() or "model"


class SearchInput(Input):
    """Search box that drops focus into its sibling table on ↓ (big-list nav)."""

    def on_key(self, event: events.Key) -> None:
        if event.key == "down":
            event.prevent_default(); event.stop()
            self.app.query_one("#model-table", DataTable).focus()


class ConfigModelsMockup(App):
    CSS = """
    $accent: #FFC933;
    $accent-dim: #FFB300;

    Screen { background: #000000; color: #E0E0E0; border: round $accent-dim; }

    #body       { height: 1fr; }                  /* fills the whole interior */
    #left       { width: 1fr; }
    #header-bar { height: 1; padding: 0 1; margin-top: 1; color: $accent; }
    #flip-tab   { height: 1; padding: 0 1; margin: 1 0; color: $accent-dim; }
    #registry   { background: #000000; height: 1fr; }
    #reg-sum    { height: 1; padding: 0 1; color: $accent-dim; }
    #affordance { height: 1; padding: 0 1; margin-top: 1; color: $accent-dim; text-style: dim; }

    #pane       { width: 40%; border-left: solid $accent-dim; padding: 1 2; }   /* full-height divider */

    /* shared right-pane chrome */
    #pane Static.pane-head { height: 2; color: $accent; }
    #pane Static.pane-hint { height: 1; color: $accent-dim; text-style: dim; }
    #pane OptionList { background: #000000; border: none; height: 1fr; padding: 0; }
    #pane OptionList > .option-list--option-highlighted { background: #1f1f1f; }
    #pane Input { height: 1; border: none; background: #000000; color: $accent; padding: 0; }

    /* PROVIDERS & KEYS (default companion) */
    #keys       { height: 1fr; }
    /* a provider's key manager */
    #keydetail  { display: none; height: 1fr; }
    #kd-note    { height: auto; color: $accent-dim; }
    #kd-list    { max-height: 8; }
    /* pin a key to a model */
    #pin        { display: none; height: 1fr; }
    #pin-note   { height: auto; color: $accent-dim; }

    /* the Provider → Auth → Model wizard */
    #wizard     { display: none; height: 1fr; }
    #providers  { height: 1fr; }
    #prov-table { background: #000000; height: 1fr; }
    #auth       { display: none; height: 1fr; }
    #auth-opts  { max-height: 6; }
    #auth-key   { margin-top: 1; }
    #auth-note  { height: 1fr; color: $accent-dim; padding: 1 0; }
    #models     { display: none; height: 1fr; }
    #model-count { height: 1; color: $accent-dim; }
    #model-table { background: #000000; height: 1fr; }
    #model-empty { display: none; height: 1fr; content-align: center middle; color: $accent-dim; }

    DataTable { background: #000000; }
    DataTable > .datatable--header { background: #000000; text-style: none; }
    DataTable > .datatable--cursor { background: #1f1f1f; }
    """

    BINDINGS = [
        Binding("f2", "cycle_theme", "Theme", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("a", "add_model", "Add model"),
        Binding("r", "remove_model", "Remove"),
        Binding("p", "pin_key", "Pin key"),
        Binding("k", "keys_pane", "Keys"),
        Binding("x", "remove_key", "Remove key"),
        Binding("o", "use_pool", "Use pool"),
        Binding("escape", "back", "Back", priority=True),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.registry = [dict(r) for r in REGISTRY_SEED]
        self.view = "keys"            # keys | keydetail | pin | providers | auth | models
        # provider-key manager state
        self.kd_prov = None
        # pin manager state
        self.pin_model = None
        # add-model wizard state
        self.sel_provider = None
        self.model_query = ""
        self.model_view: list[dict] = []

    def _c(self):
        th = THEMES[self.theme_index]
        return th["accent"], th["dim"]

    # ── layout ──────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static(id="header-bar")
                yield Static("MODELS ╶╴ agents", id="flip-tab")
                yield DataTable(id="registry", cursor_type="row", zebra_stripes=False)
                yield Static(id="reg-sum")
                yield Static(id="affordance")
            with Vertical(id="pane"):
                # default companion — PROVIDERS & KEYS
                with Vertical(id="keys"):
                    yield Static(classes="pane-head", id="keys-head")
                    yield OptionList(id="prov-keys-list")
                    yield Static("enter manage keys · a add model",
                                 classes="pane-hint", id="keys-hint")
                # a provider's key manager
                with Vertical(id="keydetail"):
                    yield Static(classes="pane-head", id="kd-head")
                    yield OptionList(id="kd-list")
                    yield Static(id="kd-note")
                    yield Input(password=True, placeholder="paste a NEW key…", id="kd-newkey")
                    yield Input(placeholder="label (optional), e.g. backup", id="kd-label")
                    yield Static("enter add · x remove selected · esc back",
                                 classes="pane-hint", id="kd-hint")
                # pin a key to a model
                with Vertical(id="pin"):
                    yield Static(classes="pane-head", id="pin-head")
                    yield OptionList(id="pin-list")
                    yield Static(id="pin-note")
                    yield Static("enter pin to model · o use pool · esc back",
                                 classes="pane-hint", id="pin-hint")
                # the add-model wizard
                with Vertical(id="wizard"):
                    yield Static(classes="pane-head", id="wiz-head")
                    with Vertical(id="providers"):
                        yield DataTable(id="prov-table", cursor_type="row", zebra_stripes=False)
                    with Vertical(id="auth"):
                        yield OptionList(id="auth-opts")
                        yield Input(placeholder="paste key…", id="auth-key")
                        yield Static(id="auth-note")
                    with Vertical(id="models"):
                        yield SearchInput(placeholder="/ filter models…", id="model-search")
                        yield Static(id="model-count")
                        yield DataTable(id="model-table", cursor_type="row", zebra_stripes=False)
                        yield Static("", id="model-empty")
                    yield Static(classes="pane-hint", id="wiz-hint")

    def on_mount(self) -> None:
        self._apply_theme()
        self._build_registry()
        self._build_provkeys()
        self._show("keys")
        self.query_one("#registry", DataTable).focus()

    # ── registry (the doorway) ────────────────────────────────────────────
    def _build_registry(self, keep: int = 0, status: str = "") -> None:
        a, d = self._c()
        dt = self.query_one("#registry", DataTable)
        dt.clear(columns=True)
        dt.add_column(Text("KEY", style=d), width=12)
        dt.add_column(Text("MODEL", style=d), width=22)
        dt.add_column(Text("AUTH", style=d), width=7)
        dt.add_column(Text("STATUS", style=d), width=12)
        for r in self.registry:
            col = a if r["ready"] else d
            dt.add_row(
                Text(r["key"], style=col),
                Text(r["model"], style=col, no_wrap=True, overflow="ellipsis"),
                Text(AUTH_SHORT.get(r["auth"], "key"), style=d),
                Text("ready ✓" if r["ready"] else "needs setup",
                     style=a if r["ready"] else d),
                key=r["key"],
            )
        if self.registry:
            dt.move_cursor(row=max(0, min(keep, len(self.registry) - 1)))
        self._render_regsum(status)

    def _render_regsum(self, status: str = "") -> None:
        a, d = self._c()
        ready = sum(1 for r in self.registry if r["ready"])
        keyed = sum(1 for p in _api_key_providers() if _n_keys(p["id"]))
        t = Text()
        if status:
            t.append(status, style=a)
        else:
            t.append(f"{len(self.registry)} models", style=a)
            t.append(f" · {ready} ready · {len(self.registry) - ready} need setup · "
                     f"{keyed} providers keyed", style=d)
        self.query_one("#reg-sum", Static).update(t)

    def _selected_model(self):
        dt = self.query_one("#registry", DataTable)
        i = dt.cursor_row
        if self.registry and i is not None and 0 <= i < len(self.registry):
            return self.registry[i]
        return None

    # ── right-pane view machine ───────────────────────────────────────────
    def _show(self, view: str) -> None:
        self.view = view
        wiz = view in ("providers", "auth", "models")
        self.query_one("#keys").display = (view == "keys")
        self.query_one("#keydetail").display = (view == "keydetail")
        self.query_one("#pin").display = (view == "pin")
        self.query_one("#wizard").display = wiz
        if wiz:
            for s in ("providers", "auth", "models"):
                self.query_one(f"#{s}").display = (s == view)
        self._set_affordance()

    def _set_affordance(self) -> None:
        txt = {
            "keys":      "↑↓ registry · a add model · r remove · p pin key · k keys · F2 theme · ^Q quit",
            "keydetail": "↑↓ keys · enter add typed key · x remove selected · esc back",
            "pin":       "↑↓ keys · enter pin to model · o use pool · esc back",
            "providers": "↑↓ · enter choose provider · esc cancel",
            "auth":      "↑↓ option · enter continue · esc back",
            "models":    "type to filter · ↓ into list · enter add · esc back",
        }[self.view]
        self.query_one("#affordance", Static).update(txt)

    # ── PROVIDERS & KEYS (default companion) ──────────────────────────────
    def _build_provkeys(self) -> None:
        a, d = self._c()
        h = Text()
        h.append("PROVIDERS & KEYS\n", style=f"{a} bold")
        h.append("select a provider to manage its keys", style=d)
        self.query_one("#keys-head", Static).update(h)
        ol = self.query_one("#prov-keys-list", OptionList)
        ol.clear_options()
        for p in _api_key_providers():
            n = _n_keys(p["id"])
            label = Text(f"{p['name']:18}", style=a)
            label.append(f"{n} key(s)" if n else "no keys yet",
                         style=a if n else d)
            ol.add_option(Option(label, id=p["id"]))

    # ── a provider's key manager ──────────────────────────────────────────
    def _show_keydetail(self, pid: str) -> None:
        a, d = self._c()
        self.kd_prov = pid
        p = PROV_BY_ID[pid]
        env = ENV_VAR.get(pid, "API_KEY")
        h = Text()
        h.append(f"Keys · {p['name']}\n", style=f"{a} bold")
        h.append(f"{env} — labels only, never the value", style=d)
        self.query_one("#kd-head", Static).update(h)
        self._fill_kd_list()
        self.query_one("#kd-newkey", Input).value = ""
        self.query_one("#kd-label", Input).value = ""
        note = Text()
        note.append(p["note"], style=d)
        self.query_one("#kd-note", Static).update(note)
        self._show("keydetail")
        self.query_one("#kd-list", OptionList).focus()

    def _fill_kd_list(self) -> None:
        a, d = self._c()
        ol = self.query_one("#kd-list", OptionList)
        ol.clear_options()
        slots = KEYPOOL.get(self.kd_prov, [])
        if not slots:
            ol.add_option(Option(Text("— no keys yet — paste one below", style=d)))
            return
        for s in slots:
            label = Text(f"#{s['index']} ", style=d)
            label.append(s["label"] or "(unlabeled)", style=a)
            if s["pinned"]:
                label.append(f"  pinned → {', '.join(s['pinned'])}", style=d)
            else:
                label.append("  shared pool", style=d)
            ol.add_option(Option(label, id=str(s["index"])))

    def action_remove_key(self) -> None:
        if self.view != "keydetail":
            return
        ol = self.query_one("#kd-list", OptionList)
        slots = KEYPOOL.get(self.kd_prov, [])
        i = ol.highlighted
        if slots and i is not None and 0 <= i < len(slots):
            victim = slots.pop(i)
            # any model pinned to it simply falls back to the shared pool
            self._fill_kd_list()
            self._build_provkeys()
            self._build_registry(keep=self.query_one("#registry", DataTable).cursor_row or 0)
            self._toast_kd(f"removed key #{victim['index']}")

    def _toast_kd(self, msg: str) -> None:
        a, d = self._c()
        p = PROV_BY_ID[self.kd_prov]
        note = Text(); note.append(f"{msg}\n", style=a); note.append(p["note"], style=d)
        self.query_one("#kd-note", Static).update(note)

    def _add_key(self) -> None:
        a, d = self._c()
        val = self.query_one("#kd-newkey", Input).value.strip()
        if not val:
            self._toast_kd("paste a key value to add"); return
        label = self.query_one("#kd-label", Input).value.strip()
        slots = KEYPOOL.setdefault(self.kd_prov, [])
        slots.append(_slot(len(slots) + 1, label, True, []))
        self.query_one("#kd-newkey", Input).value = ""
        self.query_one("#kd-label", Input).value = ""
        self._fill_kd_list()
        self._build_provkeys()
        self._toast_kd("added a key to the shared pool")

    # ── pin manager ───────────────────────────────────────────────────────
    def action_pin_key(self) -> None:
        if self.view not in ("keys",):
            return
        r = self._selected_model()
        if not r:
            return
        self.pin_model = r["key"]
        self._show_pin(r)

    def _show_pin(self, r: dict) -> None:
        a, d = self._c()
        h = Text()
        h.append("Pin key\n", style=f"{a} bold")
        h.append(f"isolate spend for '{r['key']}'", style=d)
        self.query_one("#pin-head", Static).update(h)
        note = Text()
        pl = self.query_one("#pin-list", OptionList)
        pl.clear_options()
        if r["auth"] != "api_key":
            note.append(f"'{r['key']}' uses {AUTH_SHORT.get(r['auth'])} auth —\n", style=d)
            note.append("no API key to pin. Pinning isolates a\n", style=d)
            note.append("key's spend to one model.", style=d)
        else:
            slots = KEYPOOL.get(r["prov"], [])
            if not slots:
                note.append(f"{PROV_BY_ID[r['prov']]['name']} has no keys yet —\n", style=d)
                note.append("add one in PROVIDERS & KEYS first.", style=d)
            else:
                note.append("pin a key to this model (it leaves the\n", style=d)
                note.append("shared pool), or put it back on the pool.", style=d)
                for s in slots:
                    label = Text(f"#{s['index']} ", style=d)
                    label.append(s["label"] or "(unlabeled)", style=a)
                    mine = " ◀ this model" if r["key"] in s["pinned"] else (
                        f"  → {', '.join(s['pinned'])}" if s["pinned"] else "  shared pool")
                    label.append(mine, style=d)
                    pl.add_option(Option(label, id=str(s["index"])))
        self.query_one("#pin-note", Static).update(note)
        self._show("pin")
        (pl if pl.option_count else self.query_one("#registry", DataTable)).focus()

    def _pin_selected(self, slot_index: int) -> None:
        r = next((x for x in self.registry if x["key"] == self.pin_model), None)
        if not r:
            return
        slots = KEYPOOL.get(r["prov"], [])
        for s in slots:
            if self.pin_model in s["pinned"]:
                s["pinned"].remove(self.pin_model)
        tgt = next((s for s in slots if s["index"] == slot_index), None)
        if tgt:
            tgt["pinned"].append(self.pin_model)
        self._show_pin(r)

    def action_use_pool(self) -> None:
        if self.view != "pin":
            return
        r = next((x for x in self.registry if x["key"] == self.pin_model), None)
        if not r:
            return
        for s in KEYPOOL.get(r["prov"], []):
            if self.pin_model in s["pinned"]:
                s["pinned"].remove(self.pin_model)
        self._show_pin(r)

    # ── remove a configured model ─────────────────────────────────────────
    def action_remove_model(self) -> None:
        if self.view != "keys":
            return
        r = self._selected_model()
        if not r:
            return
        self.registry = [x for x in self.registry if x["key"] != r["key"]]
        # its pinned keys rejoin the pool
        for slots in KEYPOOL.values():
            for s in slots:
                if r["key"] in s["pinned"]:
                    s["pinned"].remove(r["key"])
        self._build_registry(
            keep=self.query_one("#registry", DataTable).cursor_row or 0,
            status=f"removed '{r['key']}'")
        self._build_provkeys()

    # ── add-model wizard · step 1 providers ───────────────────────────────
    def action_add_model(self) -> None:
        if self.view not in ("keys", "keydetail", "pin"):
            return
        self._build_providers()
        self._show("providers")
        self.query_one("#prov-table", DataTable).focus()

    def _build_providers(self) -> None:
        a, d = self._c()
        h = Text()
        h.append("add model · step 1 of 3\n", style=f"{a} bold")
        h.append("choose a provider", style=d)
        self.query_one("#wiz-head", Static).update(h)
        pt = self.query_one("#prov-table", DataTable)
        pt.clear(columns=True)
        pt.add_column(Text("PROVIDER", style=d), width=16)
        pt.add_column(Text("AUTH", style=d), width=11)
        pt.add_column(Text("FREE", style=d), width=9)
        pt.add_column(Text("STATUS", style=d), width=12)
        for p in PROVIDERS:
            keyed = ("none" in p["auth"]) or any(
                s["is_set"] for s in KEYPOOL.get(p["id"], [])) or \
                any(x.startswith("oauth") for x in p["auth"])
            free_bright = p["free"] != "—"
            auth_lbl = "local" if "none" in p["auth"] else (
                "oauth · key" if any(x.startswith("oauth") for x in p["auth"]) and "api_key" in p["auth"]
                else "oauth" if any(x.startswith("oauth") for x in p["auth"]) else "key")
            pt.add_row(
                Text(p["name"], style=a, no_wrap=True, overflow="ellipsis"),
                Text(auth_lbl, style=d),
                Text(p["free"], style=a if free_bright else d),
                Text("ready ✓" if keyed else "needs setup", style=a if keyed else d),
                key=p["id"],
            )
        pt.move_cursor(row=0)

    # ── step 2 auth ────────────────────────────────────────────────────────
    def _enter_auth(self, provider: dict) -> None:
        a, d = self._c()
        self.sel_provider = provider
        h = Text()
        h.append("step 2 of 3 · authenticate\n", style=f"{a} bold")
        h.append(provider["name"], style=d)
        self.query_one("#wiz-head", Static).update(h)
        ol = self.query_one("#auth-opts", OptionList)
        ol.clear_options()
        for at in provider["auth"]:
            ol.add_option(Option(AUTH_LABEL[at], id=at))
        ol.highlighted = 0
        self._render_auth_context(provider["auth"][0])
        self._show("auth")
        ol.focus()

    def _render_auth_context(self, auth_type: str) -> None:
        a, d = self._c()
        p = self.sel_provider
        key = self.query_one("#auth-key", Input)
        t = Text()
        if auth_type == "api_key":
            key.display = True
            env = ENV_VAR.get(p["id"], "API_KEY")
            key.placeholder = f"paste {env}…"
            if _n_keys(p["id"]):
                t.append("already has a key ✓\n", style=a)
                t.append("enter to keep, or paste another.\n\n", style=d)
            else:
                t.append("paste your API key to enable it.\n\n", style=d)
            t.append(f"stored locally as {env};\nnever leaves this machine.\n", style=d)
            t.append(f"\n{p['note']}", style=d)
        elif auth_type.startswith("oauth"):
            key.display = False
            t.append("sign in with your existing account —\nno API key needed.\n\n", style=d)
            t.append("setup:  ", style=d)
            t.append(f"{OAUTH_HINT.get(auth_type, 'follow the prompt')}\n", style=a)
            t.append("\nenter to continue once signed in.", style=d)
        else:
            key.display = False
            t.append("local server · no key needed.\n\n", style=d)
            t.append("lists whatever models the server\nhas loaded.\n\n", style=d)
            t.append("enter to continue.", style=d)
        self.query_one("#auth-note", Static).update(t)

    # ── step 3 models ────────────────────────────────────────────────────────
    def _enter_models(self) -> None:
        self.model_query = ""
        self.query_one("#model-search", Input).value = ""
        a, d = self._c()
        p = self.sel_provider
        h = Text()
        h.append("step 3 of 3 · choose a model\n", style=f"{a} bold")
        h.append(p["name"], style=d)
        self.query_one("#wiz-head", Static).update(h)
        self._build_models()
        self._show("models")
        self.query_one("#model-search", Input).focus()

    def _model_view(self):
        rows = CATALOG.get(self.sel_provider["id"], [])
        q = self.model_query.strip().lower()
        if q == "free":
            rows = [m for m in rows if m["free"]]
        elif q:
            rows = [m for m in rows if q in m["id"].lower() or q in m["modality"].lower()]
        return sorted(rows, key=lambda m: (not m["pinned"],))

    def _build_models(self, keep: int = 0) -> None:
        a, d = self._c()
        self.model_view = self._model_view()
        full = CATALOG.get(self.sel_provider["id"], [])
        mt = self.query_one("#model-table", DataTable)
        empty = self.query_one("#model-empty", Static)
        mt.clear(columns=True)
        mt.add_column(Text("  MODEL", style=d), width=34)
        mt.add_column(Text("CTX", style=d), width=6)
        mt.add_column(Text("FREE", style=d), width=5)
        n_free = sum(1 for m in full if m["free"])
        true_n = TRUE_COUNT.get(self.sel_provider["id"])
        scale = f"{true_n}+ in catalog · {len(full)} shown" if true_n else f"{len(full)} models"
        cnt = Text()
        cnt.append(f"{scale} · ", style=d)
        cnt.append(f"{n_free} free", style=a if n_free else d)
        cnt.append("  ·  type to filter", style=d)
        self.query_one("#model-count", Static).update(cnt)
        if not self.model_view:
            mt.display = False; empty.display = True; empty.update("no models match"); return
        mt.display = True; empty.display = False
        for m in self.model_view:
            pin = "★ " if m["pinned"] else "  "
            label = Text(pin, style=a if m["pinned"] else d)
            label.append(m["id"], style=a)
            ctx = "—" if m["ctx"] <= 0 else f"{m['ctx']}k"
            free_cell = ("free" if m["free"] else "img" if m["modality"] == "image"
                         else "vid" if m["modality"] == "video" else "—")
            mt.add_row(label, Text(ctx, style=d),
                       Text(free_cell, style=a if m["free"] else d), key=m["id"])
        mt.move_cursor(row=max(0, min(keep, len(self.model_view) - 1)))

    def _register_model(self, model_idx: int) -> None:
        if not (0 <= model_idx < len(self.model_view)):
            return
        m = self.model_view[model_idx]
        p = self.sel_provider
        auth = "none" if "none" in p["auth"] else p["auth"][0]
        ready = (auth != "api_key") or bool(_n_keys(p["id"]))
        key = _slug(m["id"])
        existing = {r["key"] for r in self.registry}
        base = key; i = 2
        while key in existing:
            key = f"{base}-{i}"; i += 1
        self.registry.append(_reg(key, m["id"], p["id"], auth, ready))
        self._build_registry(keep=len(self.registry) - 1, status=f"added '{key}'")
        self._build_provkeys()
        self._show("keys")
        self.query_one("#registry", DataTable).focus()

    # ── navigation ─────────────────────────────────────────────────────────
    def action_keys_pane(self) -> None:
        if self.view == "keys":
            self.query_one("#prov-keys-list", OptionList).focus()

    def action_back(self) -> None:
        if self.view == "models":
            self._enter_auth(self.sel_provider)
        elif self.view == "auth":
            self._build_providers(); self._show("providers")
            self.query_one("#prov-table", DataTable).focus()
        elif self.view in ("providers", "keydetail", "pin"):
            self._show("keys")
            self.query_one("#registry", DataTable).focus()

    # ── events ──────────────────────────────────────────────────────────────
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        tid = event.data_table.id
        if tid == "prov-table" and self.view == "providers":
            self._enter_auth(PROVIDERS[event.cursor_row])
        elif tid == "model-table" and self.view == "models":
            self._register_model(event.cursor_row)

    def on_option_list_option_highlighted(self, event) -> None:
        if event.option_list.id == "auth-opts" and self.view == "auth":
            self._render_auth_context(event.option.id)

    def on_option_list_option_selected(self, event) -> None:
        lid = event.option_list.id
        if lid == "prov-keys-list" and self.view == "keys":
            self._show_keydetail(event.option.id)
        elif lid == "auth-opts" and self.view == "auth":
            if event.option.id == "api_key":
                self.query_one("#auth-key", Input).focus()
            else:
                self._enter_models()
        elif lid == "kd-list" and self.view == "keydetail":
            pass  # selection tracked via highlighted for remove
        elif lid == "pin-list" and self.view == "pin":
            self._pin_selected(int(event.option.id))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "model-search" and self.view == "models":
            self.model_query = event.value
            self._build_models()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        iid = event.input.id
        if iid == "auth-key" and self.view == "auth":
            self._enter_models()
        elif iid == "model-search" and self.view == "models":
            self._register_model(self.query_one("#model-table", DataTable).cursor_row or 0)
        elif iid in ("kd-newkey", "kd-label") and self.view == "keydetail":
            self._add_key()

    # ── theme ────────────────────────────────────────────────────────────────
    def action_cycle_theme(self) -> None:
        self.theme_index = (self.theme_index + 1) % len(THEMES)
        self._apply_theme()
        self._build_registry(keep=self.query_one("#registry", DataTable).cursor_row or 0)
        self._build_provkeys()
        if self.view in ("providers",):
            self._build_providers()
        elif self.view == "auth":
            self._render_auth_context(self.sel_provider["auth"][0])
        elif self.view == "models":
            self._build_models(keep=self.query_one("#model-table", DataTable).cursor_row or 0)
        elif self.view == "keydetail":
            self._fill_kd_list()

    def _apply_theme(self) -> None:
        a, d = self._c()
        th = THEMES[self.theme_index]
        self.screen.styles.border = ("round", d)
        self.query_one("#header-bar", Static).update(
            f"MODULATIO   config › models            feng-tui · {th['name']}")
        self.query_one("#header-bar").styles.color = a
        for sel in ("#flip-tab", "#reg-sum", "#affordance"):
            self.query_one(sel).styles.color = d
        self.query_one("#pane").styles.border_left = ("solid", d)
        for w in self.query(".pane-head"):
            w.styles.color = a
        for w in self.query(".pane-hint"):
            w.styles.color = d
        for sel in ("#kd-note", "#pin-note", "#auth-note", "#model-count"):
            self.query_one(sel).styles.color = d
        for sel in ("#auth-key", "#kd-newkey", "#kd-label", "#model-search"):
            self.query_one(sel).styles.color = a


if __name__ == "__main__":
    ConfigModelsMockup().run()
