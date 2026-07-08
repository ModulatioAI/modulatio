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

// ── AGENTS (project roster) ───────────────────────────────────────────

async function modelKeys() {
  return (await api("/config/models")).models.map((m) => m.key);
}

function mountAgents(body, ctx) {
  if (!ctx.project) {
    body.append(el("section", { class: "card", style: "max-width:520px" },
      el("p", { class: "soft" }, "No project yet — create one on the Projects tab.")));
    return;
  }
  const P = `/${ctx.project}`;
  masterDetail(body, {
    title: "Agents",
    load: async () => (await api(`${P}/config/agents`)).agents,
    columns: [
      { label: "tier", cell: (a) => a.tier, mono: true },
      { label: "id", cell: (a) => a.id, mono: true },
      { label: "name", cell: (a) => a.name },
      { label: "model", cell: (a) => a.model || "—", mono: true },
    ],
    rowLabel: (a) => `${a.tier} ${a.id} ${a.name} ${a.model}`,
    emptyText: "no agents yet — a kickoff needs a Leader, a QC, and a producer",
    actions: [
      { label: "Add agent", needsSelection: false, run: async () => {
        const models = await modelKeys();
        if (!models.length) {
          notify("Add a model on the Models tab first.", { error: true });
          return false;
        }
        const f = await formDialog("Add an agent", [
          { name: "tier", label: "Tier", options: ["producer", "leader", "qc"] },
          { name: "name", label: "Name" },
          { name: "model", label: "Model", options: models }]);
        if (!f) return false;
        await api(`${P}/config/agents`,
          { method: "POST", body: { tier: f.tier, name: f.name, model: f.model } });
        notify(`Added ${f.tier} '${f.name}'.`);
      } },
      { label: "Change model", run: async (a) => {
        const models = await modelKeys();
        const f = await formDialog(`Model for '${a.id}'`, [
          { name: "model", label: "Model", options: models, value: a.model }]);
        if (!f) return false;
        await api(`${P}/config/agents/${encodeURIComponent(a.id)}/model`,
          { method: "PUT", body: { model: f.model } });
        notify(`'${a.id}' now runs ${f.model}.`);
      } },
      { label: "Fallbacks", run: async (a) => {
        const f = await formDialog(`Fallback chain for '${a.id}'`, [
          { name: "chain", label: "Model keys, comma-separated (ordered)",
            value: (a.fallbacks || []).join(", ") }]);
        if (!f) return false;
        const chain = f.chain.split(",").map((s) => s.trim()).filter(Boolean);
        await api(`${P}/config/agents/${encodeURIComponent(a.id)}/fallbacks`,
          { method: "PUT", body: { fallbacks: chain } });
        notify(`Set ${chain.length} fallback(s) for '${a.id}'.`);
      } },
      { label: "Remove", danger: true,
        confirm: (a) => (a.tier === "producer"
          ? `Remove producer '${a.id}'?`
          : `Remove the ${a.tier.toUpperCase()} '${a.id}'? Kickoffs degrade until you re-add one.`),
        run: async (a) => {
          await api(`${P}/config/agents/${encodeURIComponent(a.id)}`,
            { method: "DELETE" });
          notify(`Removed '${a.id}'.`);
        } },
    ],
    renderDetail: (a) => [
      el("h2", {}, `${a.tier} · ${a.id}`),
      kv([["name", a.name], ["model", a.model || "—"],
        ["fallbacks", (a.fallbacks || []).join(", ") || "—"],
        ["skills", (a.skills || []).join(", ") || "—"]]),
    ],
  });
}

// ── MODELS + the write-only key manager ───────────────────────────────

function mountModels(body) {
  masterDetail(body, {
    title: "Models",
    load: async () => (await api("/config/models")).models,
    columns: [
      { label: "key", cell: (m) => m.key, mono: true },
      { label: "provider", cell: (m) => m.provider, mono: true },
      { label: "model", cell: (m) => m.model, mono: true },
      { label: "", cell: (m) => (m.available ? "● ready" : "○ needs key"), mono: true },
    ],
    rowLabel: (m) => `${m.key} ${m.provider} ${m.model}`,
    emptyText: "no models yet — add one, then add its key",
    actions: [
      { label: "Add model", needsSelection: false, run: async () => {
        const providers = (await api("/config/providers")).providers;
        const f = await formDialog("Add a model", [
          { name: "provider_id", label: "Provider",
            options: providers.map((p) => p.id) },
          { name: "model", label: "Model id (e.g. meta/llama-3, gpt-4o)" }]);
        if (!f) return false;
        await api("/config/models/add",
          { method: "POST", body: { provider_id: f.provider_id, model: f.model } });
        notify(`Added model '${f.model}'.`);
      } },
      { label: "Set key", run: async (m) => {
        if (!m.env_var) {
          notify("This model has no key slot (local / OAuth).", { error: true });
          return false;
        }
        // Write-only: the value POSTs in and never comes back. type=password so
        // it's masked in the field too.
        const f = await formDialog(`Set an API key for ${m.provider}`, [
          { name: "value", label: `Key (stored under ${m.env_var}, never shown again)`,
            type: "password" },
          { name: "label", label: "Label (optional)" }]);
        if (!f) return false;
        await api("/config/keys",
          { method: "POST", body: { base: m.env_var, value: f.value, label: f.label } });
        notify(`Key set for ${m.provider}.`);
      } },
      { label: "Remove key", danger: true, run: async (m) => {
        if (!m.env_var) return false;
        const slots = (await api(
          `/config/keys?base=${encodeURIComponent(m.env_var)}`)).slots
          .filter((s) => s.is_set);
        if (!slots.length) {
          notify("No keys set for this provider.");
          return false;
        }
        const f = await formDialog(`Remove a key for ${m.provider}`, [
          { name: "env_var", label: "Which slot",
            options: slots.map((s) => s.env_var) }]);
        if (!f) return false;
        await api(`/config/keys/${encodeURIComponent(f.env_var)}`, { method: "DELETE" });
        notify("Key removed.");
        return false;
      } },
      { label: "Remove model", danger: true,
        confirm: (m) => `Remove model '${m.key}'?`,
        run: async (m) => {
          await api(`/config/models/${encodeURIComponent(m.key)}`, { method: "DELETE" });
          notify(`Removed '${m.key}'.`);
        } },
    ],
    renderDetail: async (m) => {
      let keyLine = "no key slot (local / OAuth)";
      if (m.env_var) {
        const slots = (await api(
          `/config/keys?base=${encodeURIComponent(m.env_var)}`)).slots;
        const n = slots.filter((s) => s.is_set).length;
        keyLine = `${n} key(s) set under ${m.env_var}`;
      }
      return [
        el("h2", {}, m.key),
        kv([["provider", m.provider], ["model", m.model],
          ["status", m.available ? "● ready" : "○ needs a key"],
          ["keys", keyLine]]),
      ];
    },
  });
}

// ── SERVICES (outside APIs; keys via the same write-only pool) ────────

function mountServices(body) {
  masterDetail(body, {
    title: "Services",
    load: async () => (await api("/config/services")).services,
    columns: [
      { label: "service", cell: (s) => s.name },
      { label: "kind", cell: (s) => s.kind, mono: true },
      { label: "does", cell: (s) => s.capabilities.join(", "), mono: true },
    ],
    rowLabel: (s) => `${s.name} ${s.kind} ${s.capabilities.join(" ")}`,
    emptyText: "no services — add one from the catalog or a custom API",
    actions: [
      { label: "Add from catalog", needsSelection: false, run: async () => {
        const cat = (await api("/config/service-catalog")).catalog;
        const f = await formDialog("Add a catalog service", [
          { name: "catalog_id", label: "Service",
            options: cat.map((e) => e.id) }]);
        if (!f) return false;
        await api("/config/services/add-catalog",
          { method: "POST", body: { catalog_id: f.catalog_id } });
        notify(`Added '${f.catalog_id}'. Set its key next.`);
      } },
      { label: "Add custom", needsSelection: false, run: async () => {
        const f = await formDialog("Add a custom service", [
          { name: "name", label: "Name" },
          { name: "base_url", label: "Base URL (https:// — pinned; the pin is the auth)" },
          { name: "auth_shape", label: "Auth: bearer | header:<Name> | query:<name>",
            value: "bearer" },
          { name: "capabilities", label: "Capabilities, comma-separated (image, research…)" }]);
        if (!f) return false;
        const caps = f.capabilities.split(",").map((s) => s.trim()).filter(Boolean);
        await api("/config/services/add-custom", { method: "POST", body: {
          name: f.name, base_url: f.base_url, auth_shape: f.auth_shape,
          capabilities: caps } });
        notify(`Added '${f.name}'. Set its key next.`);
      } },
      { label: "Set key", run: async (s) => {
        const f = await formDialog(`Set the API key for ${s.name}`, [
          { name: "value", label: "Key (write-only, never shown again)",
            type: "password" }]);
        if (!f) return false;
        await api("/config/keys",
          { method: "POST", body: { base: s.env_var, value: f.value, label: s.name } });
        notify(`Key set for ${s.name}.`);
        return false;
      } },
      { label: "Make default", run: async (s) => {
        const f = await formDialog(`Default ${s.name} for which capability?`, [
          { name: "capability", label: "Capability", options: s.capabilities }]);
        if (!f) return false;
        await api("/config/services/default", { method: "POST",
          body: { capability: f.capability, service_id: s.id } });
        notify(`${s.name} is now the default for ${f.capability}.`);
        return false;
      } },
      { label: "Remove", danger: true,
        confirm: (s) => `Remove service '${s.name}'?`,
        run: async (s) => {
          await api(`/config/services/${encodeURIComponent(s.id)}`, { method: "DELETE" });
          notify(`Removed '${s.name}'.`);
        } },
    ],
    renderDetail: async (s) => {
      const slots = (await api(
        `/config/keys?base=${encodeURIComponent(s.env_var)}`)).slots;
      const n = slots.filter((x) => x.is_set).length;
      return [
        el("h2", {}, s.name),
        kv([["kind", s.kind], ["capabilities", s.capabilities.join(", ")],
          ["base url", s.base_url], ["per-task cap", s.per_task_cap],
          ["keys", `${n} set`]]),
      ];
    },
  });
}

const SUBPAGES = {
  models: mountModels,
  agents: mountAgents,
  services: mountServices,
  folders: mountFolders,
  projects: mountProjects,
  settings: mountSettings,
};
