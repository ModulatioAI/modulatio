// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
// The shell: top bar, tab rail, hash router, page mount. Pages register
// in PAGES and receive (container, ctx) — ctx carries the active
// project and shared helpers. The TUI's tabs, in the TUI's order.

import { api } from "./api.js";
import { el } from "./dom.js";
import { mountConsole } from "./pages/console.js";
import {
  ATELIER_FIELDS, cycleTheme, loadTheme, setTheme, themeLabel, themeState,
} from "./theme.js";

const TABS = [
  ["console", "CONSOLE"],
  ["config", "CONFIG"],
  ["jts", "JT LIBRARY"],
  ["tickets", "TICKETS"],
  ["artifacts", "ARTIFACTS"],
  ["skills", "SKILLS"],
  ["memory", "MEMORY"],
  ["jobs", "JOBS"],
  ["cron", "CRON"],
  ["logs", "LOGS"],
  ["docs", "DOCS"],
];

// Pages by tab id — each mount receives (container, ctx) and may return
// an unmount cleanup.
export const PAGES = { console: mountConsole };

const ctx = { project: null, projects: [] };
let unmount = null;

function renderBreadcrumb() {
  const code = ctx.project ? ctx.project.toUpperCase() : "—";
  document.getElementById("breadcrumb").textContent =
    `:: PROJECT ${code} :: feng-web · ${themeLabel()}`;
}

function renderThemeControls() {
  const host = document.getElementById("theme-controls");
  host.replaceChildren();
  const st = themeState();
  if (st.theme === "atelier") {
    for (const f of ATELIER_FIELDS) {
      host.append(el("button", {
        class: "swatch",
        title: `field: ${f}`,
        "aria-label": `field ${f}`,
        "aria-pressed": String(st.field === f),
        style: `background:${swatchColor(f)}`,
        onclick: () => setTheme({ field: f }),
      }));
    }
  } else {
    host.append(
      el("button", {
        class: "btn", onclick: () => setTheme({
          vellum: st.vellum === "standard" ? "inverted" : "standard",
        }),
      }, st.vellum === "standard" ? "◐ invert" : "◑ revert"),
      el("button", {
        class: "btn", onclick: () => setTheme({
          vfield: st.vfield === "sage" ? "grey" : "sage",
        }),
      }, st.vfield === "sage" ? "paper: sage" : "paper: grey"),
    );
  }
  host.append(el("button", {
    class: "btn", title: "F2 cycles themes", onclick: cycleTheme,
  }, st.theme === "atelier" ? "ATELIER" : "VELLUM"));
}

function swatchColor(f) {
  return {
    sage: "#BFC8BD", reed: "#BCD9C2", mist: "#B8D4DB",
    clay: "#D9BDB8", heather: "#CCC2DB", bone: "#D9D4C8",
  }[f];
}

function currentTab() {
  const hash = location.hash.replace(/^#\/?/, "");
  return TABS.some(([id]) => id === hash) ? hash : "console";
}

function renderTabs() {
  const rail = document.getElementById("tabrail");
  rail.replaceChildren(...TABS.map(([id, label]) => el("button", {
    class: "tab",
    "aria-current": currentTab() === id ? "page" : null,
    onclick: () => { location.hash = `#/${id}`; },
  }, label)));
}

async function mountPage() {
  const page = document.getElementById("page");
  if (unmount) { unmount(); unmount = null; }
  page.replaceChildren();
  renderTabs();
  const tab = currentTab();
  const mount = PAGES[tab] ?? placeholderPage(tab);
  unmount = (await mount(page, ctx)) ?? null;
}

function placeholderPage(tab) {
  return (page) => {
    page.append(el("section", { class: "card", style: "max-width:520px" },
      el("h2", {}, TABS.find(([id]) => id === tab)?.[1] ?? tab),
      el("p", { class: "soft" },
        "This page arrives with a later phase of the WebOS build."),
    ));
  };
}

async function boot() {
  loadTheme();
  document.addEventListener("themechange", () => {
    renderThemeControls();
    renderBreadcrumb();
  });
  window.addEventListener("hashchange", mountPage);
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "F2") { ev.preventDefault(); cycleTheme(); }
  });

  try {
    const data = await api("/projects");
    ctx.projects = data.projects;
    ctx.project = data.default ?? data.projects[0] ?? null;
  } catch (err) {
    ctx.error = err;
  }
  renderThemeControls();
  renderBreadcrumb();
  document.getElementById("hints").innerHTML =
    `<span class="key">F2</span> theme · <span class="key">Esc</span> interrupt · ` +
    `<span class="key">F8</span> stop run`;
  await mountPage();
}

boot();
