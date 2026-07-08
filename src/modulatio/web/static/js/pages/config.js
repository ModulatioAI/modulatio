// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
// The CONFIG tab shell — an inner sub-tab row (Models · Agents · Services ·
// Folders · Projects · Settings) over one body. Each sub-page is a
// MasterDetail binding (the archetype + action-row already carries the
// Configurator's job: a registry list you operate on). Sub-pages arrive
// across the Feature-2 build; unbuilt ones show a placeholder.

import { api } from "../api.js";
import { el } from "../dom.js";
import { formDialog, kv, masterDetail, notify } from "./masterdetail.js";

const SUBTABS = [
  ["models", "MODELS"],
  ["agents", "AGENTS"],
  ["services", "SERVICES"],
  ["folders", "FOLDERS"],
  ["projects", "PROJECTS"],
  ["settings", "SETTINGS"],
];

export function mountConfig(page, ctx) {
  let active = "settings";
  const rail = el("nav", { class: "row subtab-row", "aria-label": "config sections" });
  const body = el("div", {});

  function renderRail() {
    rail.replaceChildren(...SUBTABS.map(([id, label]) => el("button", {
      class: "tab subtab",
      "aria-current": id === active ? "page" : null,
      onclick: () => { active = id; mountSub(); },
    }, label)));
  }

  function mountSub() {
    renderRail();
    body.replaceChildren();
    (SUBPAGES[active] ?? placeholder(active))(body, ctx);
  }

  page.append(rail, body);
  mountSub();
}

function placeholder(id) {
  return (body) => body.append(el("section", { class: "card", style: "max-width:520px" },
    el("p", { class: "soft" },
      `CONFIG · ${id.toUpperCase()} — arriving in this build.`)));
}

// ── SETTINGS ──────────────────────────────────────────────────────────

function mountSettings(body) {
  masterDetail(body, {
    title: "Settings",
    load: async () => (await api("/settings")).knobs,
    columns: [
      { label: "setting", cell: (k) => k.label },
      { label: "value", cell: (k) => k.value || "(default)", mono: true },
      { label: "source", cell: (k) => k.source, mono: true },
    ],
    rowLabel: (k) => `${k.label} ${k.key} ${k.value} ${k.source}`,
    emptyText: "no adjustable settings",
    actions: [
      { label: "Edit", run: async (k) => {
        if (k.source === "shell/.env") {
          notify("Owned by your shell/.env — read-only here.", { error: true });
          return false;
        }
        const f = await formDialog(`Edit ${k.label}`, [
          { name: "value", label: `${k.hint}  (default: ${k.default || "blank"})`,
            value: k.value }]);
        if (!f) return false;
        await api(`/settings/${encodeURIComponent(k.key)}`,
          { method: "POST", body: { value: f.value } });
        notify("Saved — applies to the next call/run.");
      } },
      { label: "Clear override", danger: true,
        confirm: (k) => `Clear '${k.label}' back to the shipped default?`,
        run: async (k) => {
          await api(`/settings/${encodeURIComponent(k.key)}`, { method: "DELETE" });
          notify("Override cleared — default restored.");
        } },
    ],
    renderDetail: (k) => [
      el("h2", {}, k.label),
      el("p", { class: "soft" }, k.hint),
      kv([["key", k.key], ["value", k.value || "(default)"],
        ["default", k.default || "(project decides)"], ["source", k.source]]),
    ],
  });
}

const SUBPAGES = {
  settings: mountSettings,
};
