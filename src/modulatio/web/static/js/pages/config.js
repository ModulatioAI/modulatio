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

// ── FOLDERS ───────────────────────────────────────────────────────────

function mountFolders(body) {
  masterDetail(body, {
    title: "Folders",
    load: async () => (await api("/config/folders")).folders,
    columns: [
      { label: "name", cell: (f) => f.name, mono: true },
      { label: "mode", cell: (f) => f.mode, mono: true },
      { label: "path", cell: (f) => f.path, mono: true },
    ],
    rowLabel: (f) => `${f.name} ${f.mode} ${f.path}`,
    emptyText: "no folders registered — add one the team can use by name",
    actions: [
      { label: "Add folder", needsSelection: false, run: async () => {
        const f = await formDialog("Register a folder", [
          { name: "name", label: "Name (used in kickoff directions)" },
          { name: "path", label: "Absolute path (a mounted share's mount point)" },
          { name: "mode", label: "Mode", options: ["ro", "output", "rw"] }]);
        if (!f) return false;
        await api("/config/folders",
          { method: "POST", body: { name: f.name, path: f.path, mode: f.mode } });
        notify(`Registered '${f.name}'.`);
      } },
      { label: "Set as output", run: async (f) => {
        await api(`/config/folders/${encodeURIComponent(f.name)}/output`,
          { method: "POST" });
        notify(`'${f.name}' will receive the job's finished product.`);
        return false;
      } },
      { label: "Remove", danger: true,
        confirm: (f) => `Unregister folder '${f.name}'?`,
        run: async (f) => {
          await api(`/config/folders/${encodeURIComponent(f.name)}`,
            { method: "DELETE" });
          notify(`Removed '${f.name}'.`);
        } },
    ],
    renderDetail: (f) => [
      el("h2", {}, f.name),
      kv([["mode", f.mode], ["path", f.path]]),
    ],
  });
}

// ── PROJECTS ──────────────────────────────────────────────────────────

function mountProjects(body, ctx) {
  masterDetail(body, {
    title: "Projects",
    load: async () => {
      const d = await api("/projects");
      return d.projects.map((code) => ({ code, active: code === d.default }));
    },
    columns: [
      { label: "project", cell: (p) => p.code, mono: true },
      { label: "", cell: (p) => (p.active ? "● active" : ""), mono: true },
    ],
    rowLabel: (p) => p.code,
    emptyText: "no projects yet — create one to start",
    actions: [
      { label: "New project", needsSelection: false, run: async () => {
        const f = await formDialog("New project", [
          { name: "code", label: "Code (lowercase letters, digits, _)" },
          { name: "objective", label: "Objective (optional)" }]);
        if (!f) return false;
        await api("/projects",
          { method: "POST", body: { code: f.code, objective: f.objective } });
        notify(`Created '${f.code}'.`);
      } },
      { label: "Switch to", run: async (p) => {
        await api(`/projects/${encodeURIComponent(p.code)}/switch`,
          { method: "POST" });
        ctx.project = p.code;
        notify(`Switched to '${p.code}'.`);
      } },
      { label: "Delete", danger: true,
        confirm: (p) => `Delete project '${p.code}'? (It is backed up first.)`,
        run: async (p) => {
          await api(`/projects/${encodeURIComponent(p.code)}`, { method: "DELETE" });
          notify(`Deleted '${p.code}' (backed up).`);
        } },
    ],
    renderDetail: (p) => [
      el("h2", {}, p.code),
      el("p", { class: "soft" }, p.active ? "The active project." : "Not active."),
    ],
  });
}

const SUBPAGES = {
  settings: mountSettings,
  folders: mountFolders,
  projects: mountProjects,
};
