// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
// The nine read-only MasterDetail pages — pure data bindings over the
// archetype. Where the TUI has a tab, the web has a binding.

import { api } from "../api.js";
import { el } from "../dom.js";
import { heading, kv, masterDetail, pre } from "./masterdetail.js";

const P = (ctx) => `/${ctx.project}`;

function needsProject(page, ctx) {
  if (ctx.project) return false;
  page.append(el("section", { class: "card", style: "max-width:520px" },
    el("p", { class: "soft" }, "No project yet — create one first.")));
  return true;
}

export function mountJts(page, ctx) {
  if (needsProject(page, ctx)) return;
  masterDetail(page, {
    title: "JT Library",
    load: async () => (await api(`${P(ctx)}/jts`)).jts,
    columns: [
      { label: "template", cell: (j) => j.name, mono: true },
      { label: "description", cell: (j) => j.description },
    ],
    rowLabel: (j) => `${j.name} ${j.description}`,
    renderDetail: async (j) => {
      const full = await api(`${P(ctx)}/jts/${j.name}`);
      return [
        heading(full.name),
        el("p", {}, full.description),
        kv([
          ["capabilities", (full.capability_preferences ?? []).join(", ") || "—"],
          ["params", (full.param_schema ?? []).map((p) => p.name).join(", ") || "—"],
          ["version", full.version],
        ]),
        pre(full.interview_body),
      ];
    },
  });
}

export function mountTickets(page, ctx) {
  if (needsProject(page, ctx)) return;
  masterDetail(page, {
    title: "Tickets",
    load: async () => (await api(`${P(ctx)}/tickets`)).tickets,
    columns: [
      { label: "id", cell: (t) => t.id, mono: true },
      { label: "priority", cell: (t) => t.priority, mono: true },
      { label: "status", cell: (t) => t.status, mono: true },
      { label: "title", cell: (t) => t.title },
    ],
    rowLabel: (t) => `${t.id} ${t.priority} ${t.status} ${t.title}`,
    emptyText: "no tickets — the floor is quiet",
    renderDetail: (t) => [
      heading(`${t.id} — ${t.title}`),
      kv([
        ["priority", t.priority], ["status", t.status],
        ["goal", t.affected_goal_id], ["task", t.affected_task_id],
        ["run", t.run_id],
      ]),
      pre(t.body),
    ],
  });
}

export function mountJobs(page, ctx) {
  if (needsProject(page, ctx)) return;
  masterDetail(page, {
    title: "Jobs",
    load: async () => (await api(`${P(ctx)}/runs`)).runs,
    columns: [
      { label: "run", cell: (r) => r.run_id, mono: true },
      { label: "size", cell: (r) => r.size_human, mono: true },
    ],
    rowLabel: (r) => r.run_id,
    emptyText: "no runs yet — kick one off from the Console",
    renderDetail: async (r) => {
      const [detail, tasks] = await Promise.all([
        api(`${P(ctx)}/runs/${r.run_id}`),
        api(`${P(ctx)}/runs/${r.run_id}/tasks`),
      ]);
      const byStatus = {};
      for (const t of tasks.tasks) {
        byStatus[t.status] = (byStatus[t.status] ?? 0) + 1;
      }
      return [
        heading(r.run_id),
        kv([
          ...Object.entries(detail.counts).map(([k, v]) => [k, v]),
          ["size", detail.size_human],
          ["tasks", Object.entries(byStatus)
            .map(([s, n]) => `${s}: ${n}`).join(" · ") || "0"],
        ]),
        pre(detail.objective),
      ];
    },
  });
}

export function mountLogs(page, ctx) {
  if (needsProject(page, ctx)) return;
  masterDetail(page, {
    title: "Logs",
    load: async () => (await api("/logs")).logs,
    columns: [
      { label: "kind", cell: (l) => l.label, mono: true },
      { label: "when", cell: (l) => l.timestamp, mono: true },
      { label: "sent", cell: (l) => (l.sent ? "✓ sent" : "—"), mono: true },
    ],
    rowLabel: (l) => `${l.kind} ${l.timestamp} ${l.summary}`,
    emptyText: "no crash / error / doctor logs — good sign",
    renderDetail: (l) => [
      heading(l.label),
      kv([["when", l.timestamp], ["size", l.size_human], ["sent", l.sent]]),
      pre(l.summary),
    ],
  });
}

export function mountSkills(page, ctx) {
  if (needsProject(page, ctx)) return;
  masterDetail(page, {
    title: "Skills",
    load: async () => (await api(`${P(ctx)}/skills`)).skills.map((s) => ({ name: s })),
    columns: [{ label: "skill", cell: (s) => s.name, mono: true }],
    rowLabel: (s) => s.name,
    renderDetail: async (s) => {
      const full = await api(`${P(ctx)}/skills/${s.name}`);
      return [heading(s.name), pre(full.body || "(empty prompt template)")];
    },
  });
}

export function mountArtifacts(page, ctx) {
  if (needsProject(page, ctx)) return;
  masterDetail(page, {
    title: "Artifacts",
    load: async () => (await api(`${P(ctx)}/artifacts`)).files,
    columns: [
      // ★ marks the finished product — the deliverable the operator asked
      // for — hoisted above the research/draft pile (the TUI contract).
      { label: "", cell: (f) => (f.product ? "★ " : "") + f.family_glyph,
        mono: true },
      { label: "path", cell: (f) => f.path, mono: true },
      { label: "size", cell: (f) => f.size_human, mono: true },
    ],
    rowLabel: (f) => (f.product ? "★ " : "") + f.path,
    emptyText: "no artifacts yet — they land here as runs produce",
    renderDetail: async (f) => {
      const prev = await api(
        `${P(ctx)}/artifacts/preview?path=${encodeURIComponent(f.path)}`);
      return [heading(f.path), pre(prev.text)];
    },
  });
}

export function mountMemory(page, ctx) {
  if (needsProject(page, ctx)) return;
  // One row shape for all three sources, mirroring the TUI's memory
  // table (Layer/When/Kind/Content); `raw` keeps the full entry for the
  // detail pane. Kind follows the TUI: team → artifact kind, episodic →
  // entry type, semantic → confidence.
  const row = (layer, agent, when, kind, body, raw) =>
    ({ layer, agent, when, kind: kind || "?", body: body ?? "", raw });
  masterDetail(page, {
    title: "Memory",
    load: async () => {
      const m = await api(`${P(ctx)}/memory`);
      return [
        ...m.proposals.map((p) =>
          row("pending", "", p.timestamp, p.artifact_kind, p.body, p)),
        ...m.entries.map((e) =>
          row("team", "", e.timestamp, e.artifact_kind, e.body, e)),
        ...m.agent_entries.map((e) =>
          row(e.layer, e.agent_id, e.when,
            e.layer === "semantic" ? e.confidence : e.type, e.content, e)),
      ];
    },
    columns: [
      { label: "layer", cell: (m) => m.layer, mono: true },
      { label: "agent", cell: (m) => m.agent || "—", mono: true },
      { label: "kind", cell: (m) => m.kind, mono: true },
      { label: "memory", cell: (m) => m.body.split("\n")[0].slice(0, 96) },
    ],
    rowLabel: (m) => `${m.layer} ${m.agent} ${m.kind} ${m.body}`,
    emptyText: "nothing remembered yet",
    renderDetail: (m) => [
      heading(m.agent ? `${m.layer} · ${m.agent}` : m.layer),
      kv(Object.entries(m.raw)),
    ],
  });
}

export function mountCron(page, ctx) {
  if (needsProject(page, ctx)) return;
  masterDetail(page, {
    title: "Cron",
    load: async () => (await api(`${P(ctx)}/cron`)).jobs,
    columns: [
      { label: "name", cell: (j) => j.name, mono: true },
      { label: "schedule", cell: (j) => j.schedule, mono: true },
      { label: "enabled", cell: (j) => (j.enabled ? "● on" : "○ off"), mono: true },
    ],
    rowLabel: (j) => `${j.name} ${j.schedule} ${j.objective}`,
    emptyText: "no scheduled jobs",
    renderDetail: (j) => [
      heading(j.name),
      kv([
        ["schedule", j.schedule], ["enabled", j.enabled ? "● on" : "○ off"],
        ["next run", j.next_run], ["priority", j.priority],
        ["job template", j.jt_id],
      ]),
      pre(j.objective),
    ],
  });
}

export function mountDocs(page, ctx) {
  masterDetail(page, {
    title: "Docs",
    wideDetail: true,
    load: async () => (await api("/docs")).docs,
    columns: [{ label: "page", cell: (d) => d.title }],
    rowLabel: (d) => `${d.slug} ${d.title}`,
    renderDetail: async (d) => {
      const page_ = await api(`/docs/${d.slug}`);
      return [heading(d.title), pre(page_.markdown)];
    },
  });
}
