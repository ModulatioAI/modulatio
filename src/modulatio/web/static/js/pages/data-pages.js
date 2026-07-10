// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
// The nine read-only MasterDetail pages — pure data bindings over the
// archetype. Where the TUI has a tab, the web has a binding.

import { api } from "../api.js";
import { el } from "../dom.js";
import {
  formDialog, heading, kv, masterDetail, notify, pre,
} from "./masterdetail.js";

// Small helpers so each page's actions stay one-liners.
const post = (path, body) => api(path, { method: "POST", body });
const del = (path) => api(path, { method: "DELETE" });

// ── the schedule builder (date · time · recurrence · ends) ──
const _WD = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
const _WD_LONG = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
  "Friday", "Saturday"];

function _prettyDateTime(dateStr, timeStr) {
  const d = new Date(`${dateStr}T${timeStr}`);
  return d.toLocaleString(undefined,
    { dateStyle: "medium", timeStyle: "short" });
}

// Resolve the builder's fields into {schedule, start_at, count, until} + a
// human sentence. Returns { spec, sentence } or { error }.
function _buildSchedule(f) {
  if (!f.date || !f.time) return { error: "Pick a date and a time." };
  const start_at = `${f.date}T${f.time}:00`;
  const when = _prettyDateTime(f.date, f.time);
  const d = new Date(`${f.date}T${f.time}`);
  const [hh, mm] = f.time.split(":");
  if (!f.recurring) {
    return { spec: { schedule: "once", start_at, count: 1, until: null },
      sentence: `Runs once on ${when}.` };
  }
  let schedule, freqText;
  if (f.freq === "Daily") { schedule = `daily ${hh}:${mm}`; freqText = "daily"; }
  else if (f.freq === "Weekly") {
    schedule = `weekly ${_WD[d.getDay()]} ${hh}:${mm}`;
    freqText = `weekly on ${_WD_LONG[d.getDay()]}s`;
  } else if (f.freq === "Monthly") {
    schedule = `monthly ${d.getDate()} ${hh}:${mm}`;
    freqText = `monthly on day ${d.getDate()}`;
  } else if (f.freq === "Every N days") {
    const n = Math.max(1, parseInt(f.everyN, 10) || 1);
    schedule = `${n}d`; freqText = n === 1 ? "every day" : `every ${n} days`;
  } else { // Every N weeks
    const n = Math.max(1, parseInt(f.everyN, 10) || 1);
    schedule = `${n * 7}d`; freqText = n === 1 ? "every week" : `every ${n} weeks`;
  }
  let count = null, until = null, endsText = "";
  if (f.ends === "After N runs") {
    count = Math.max(1, parseInt(f.endsN, 10) || 1);
    endsText = `, ${count} times`;
  } else if (f.ends === "On date") {
    if (!f.endsDate) return { error: "Pick an end date (or choose Never)." };
    until = f.endsDate;
    endsText = `, until ${new Date(f.endsDate + "T00:00").toLocaleDateString(undefined, { dateStyle: "medium" })}`;
  }
  return { spec: { schedule, start_at, count, until },
    sentence: `Runs ${freqText} at ${hh}:${mm}, starting ${when}${endsText}.` };
}

function scheduleDialog(jtName) {
  return new Promise((resolve) => {
    const inp = (attrs) => el("input", { class: "form-input", ...attrs });
    const opts = (arr) => arr.map((o) => el("option", {}, o));
    const dateIn = inp({ type: "date" });
    const timeIn = inp({ type: "time", value: "09:00" });
    const recurCk = el("input", { type: "checkbox" });
    const freqSel = el("select", { class: "form-input" },
      ...opts(["Daily", "Weekly", "Monthly", "Every N days", "Every N weeks"]));
    const everyN = inp({ type: "number", min: "1", value: "1" });
    const endsSel = el("select", { class: "form-input" },
      ...opts(["Never", "After N runs", "On date"]));
    const endsN = inp({ type: "number", min: "1", value: "5" });
    const endsDate = inp({ type: "date" });
    const preview = el("p", { class: "soft mono" });

    const field = (label, node) =>
      el("label", { class: "form-field" }, el("span", {}, label), node);
    const rRecur = field("Repeat", recurCk);
    const rFreq = field("Frequency", freqSel);
    const rEvery = field("Every (N)", everyN);
    const rEnds = field("Ends", endsSel);
    const rEndsN = field("After (runs)", endsN);
    const rEndsDate = field("End date", endsDate);

    const collect = () => ({
      date: dateIn.value, time: timeIn.value, recurring: recurCk.checked,
      freq: freqSel.value, everyN: everyN.value, ends: endsSel.value,
      endsN: endsN.value, endsDate: endsDate.value,
    });
    const show = (node, on) => { node.hidden = !on; };
    function refresh() {
      const rec = recurCk.checked;
      show(rFreq, rec);
      show(rEvery, rec && freqSel.value.startsWith("Every"));
      show(rEnds, rec);
      show(rEndsN, rec && endsSel.value === "After N runs");
      show(rEndsDate, rec && endsSel.value === "On date");
      const r = _buildSchedule(collect());
      preview.textContent = r.error ? `⚠ ${r.error}` : `↻ ${r.sentence}`;
    }
    for (const n of [dateIn, timeIn, recurCk, freqSel, everyN, endsSel, endsN, endsDate]) {
      n.addEventListener("change", refresh);
      n.addEventListener("input", refresh);
    }

    const dlg = el("dialog", { class: "approval card" },
      el("h2", {}, `Schedule '${jtName}'`),
      field("Date", dateIn), field("Time", timeIn),
      rRecur, rFreq, rEvery, rEnds, rEndsN, rEndsDate,
      preview,
      el("div", { class: "row", style: "justify-content:flex-end" },
        el("button", { class: "btn", onclick: () => { dlg.close(); resolve(null); } }, "Cancel"),
        el("button", {
          class: "btn btn--primary",
          onclick: () => {
            const r = _buildSchedule(collect());
            if (r.error) { preview.textContent = `⚠ ${r.error}`; return; }
            dlg.close();
            resolve(r);
          },
        }, "Schedule")));
    document.body.append(dlg);
    dlg.addEventListener("close", () => dlg.remove());
    refresh();
    dlg.showModal();
  });
}

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
    actions: [
      { label: "Kick off", run: async (j) => {
        const r = await post(`${P(ctx)}/jts/${encodeURIComponent(j.name)}/kickoff`);
        notify(`Kicked off '${j.name}' — run ${r.run_id}. Watch the Console.`);
        return false;  // no list change
      } },
      { label: "Schedule…", run: async (j) => {
        const r = await scheduleDialog(j.name);
        if (!r) return false;
        await post(`${P(ctx)}/jts/${encodeURIComponent(j.name)}/schedule`, r.spec);
        notify(`Scheduled '${j.name}' — ${r.sentence}`);
        return false;
      } },
    ],
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
    actions: [
      { label: "Delete", danger: true,
        confirm: (t) => `Delete ticket ${t.id}?`,
        run: async (t) => {
          await del(`${P(ctx)}/tickets/${encodeURIComponent(t.id)}`);
          notify(`Deleted ${t.id}.`);
        } },
    ],
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
    actions: [
      { label: "Reveal folder", run: async (r) => {
        const x = await post(`${P(ctx)}/runs/${r.run_id}/reveal`);
        notify(x.path);
        return false;
      } },
      { label: "Delete", danger: true,
        confirm: (r) => `Delete run ${r.run_id}? This is permanent.`,
        run: async (r) => {
          await del(`${P(ctx)}/runs/${r.run_id}`);
          notify("Deleted the run.");
        } },
    ],
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
    actions: [
      { label: "Send to team", run: async (l) => {
        const x = await post(`/logs/${l.id}/send`);
        window.open(x.url, "_blank", "noopener");
        notify("Opened a prefilled issue in a new tab.");
      } },
      { label: "Delete", danger: true, confirm: "Delete this log for good?",
        run: async (l) => { await del(`/logs/${l.id}`); notify("Deleted."); } },
      { label: "Refresh", needsSelection: false, run: async () => {} },
    ],
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
    actions: [
      { label: "Add skill", needsSelection: false, run: async () => {
        const f = await formDialog("New skill", [
          { name: "name", label: "Name (kebab-case)" },
          { name: "description", label: "Description" },
          { name: "prompt_template", label: "Prompt template", textarea: true }]);
        if (!f) return false;
        await post(`${P(ctx)}/skills`, f);
        notify(`Added '${f.name}'.`);
      } },
      { label: "Delete", danger: true,
        confirm: (s) => `Delete skill '${s.name}'?`,
        run: async (s) => {
          await del(`${P(ctx)}/skills/${encodeURIComponent(s.name)}`);
          notify(`Deleted '${s.name}'.`);
        } },
    ],
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
    actions: [
      { label: "Export…", run: async (f) => {
        const folders = (await api("/folders")).folders
          .filter((x) => x.mode === "rw" || x.mode === "output");
        if (!folders.length) {
          notify("Register a writable folder on the Folders tab first.",
            { error: true });
          return false;
        }
        const form = await formDialog(`Export '${f.path.split("/").pop()}'`, [
          { name: "format", label: "Format", options: ["docx", "pdf", "markdown"] },
          { name: "folder", label: "To folder", options: folders.map((x) => x.name) }]);
        if (!form) return false;
        const r = await post(`${P(ctx)}/artifacts/export`,
          { path: f.path, format: form.format, folder: form.folder });
        notify(`Exported ${r.filename} to folder '${r.folder}'.`);
        return false;
      } },
      { label: "Delete", danger: true,
        confirm: (f) => `Delete '${f.path}'?`,
        run: async (f) => {
          await del(`${P(ctx)}/artifacts?path=${encodeURIComponent(f.path)}`);
          notify("Deleted.");
        } },
    ],
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
    // Verbs mirror the TUI: an operator manages an AGENT's own memory in
    // place, while TEAM memory is QC-curated (edits propose, never write).
    actions: [
      { label: "Add", needsSelection: false, run: async () => {
        const f = await formDialog("Add a semantic memory to an agent", [
          { name: "agent_id", label: "Agent id" },
          { name: "content", label: "Memory", textarea: true }]);
        if (!f) return false;
        await post(`${P(ctx)}/memory/agent/${encodeURIComponent(f.agent_id)}`,
          { content: f.content });
        notify(`Added a memory for '${f.agent_id}'.`);
      } },
      { label: "Edit", run: async (m) => {
        if (m.layer === "pending") {
          notify("A pending proposal isn't editable — approve or reject it.");
          return false;
        }
        if (m.layer === "team") {
          const f = await formDialog("Propose a revision (QC reviews it)", [
            { name: "body", label: "Revision", textarea: true, value: m.body }]);
          if (!f) return false;
          await post(`${P(ctx)}/memory/propose`, { body: f.body });
          notify("Proposed a revision — QC will review it.");
          return false;
        }
        const f = await formDialog(`Edit this ${m.layer} memory of '${m.agent}'`, [
          { name: "content", label: "Memory", textarea: true, value: m.body }]);
        if (!f) return false;
        await api(
          `${P(ctx)}/memory/agent/${encodeURIComponent(m.agent)}/${m.raw.id}`,
          { method: "PUT", body: { layer: m.layer, content: f.content } });
        notify("Edited.");
      } },
      { label: "Approve", run: async (m) => {
        if (m.layer !== "pending") {
          notify("Only pending proposals are approvable.");
          return false;
        }
        await post(`${P(ctx)}/memory/proposals/${m.raw.proposal_id}/approve`);
        notify("Approved — it's team memory now.");
      } },
      { label: "Delete", danger: true,
        confirm: (m) => m.layer === "pending"
          ? "Reject this pending proposal?"
          : `Delete this ${m.layer} memory of '${m.agent}'?`,
        run: async (m) => {
          if (m.layer === "team") {
            notify("Team memory is QC-curated — revise it via Edit (propose).");
            return false;
          }
          if (m.layer === "pending") {
            await post(`${P(ctx)}/memory/proposals/${m.raw.proposal_id}/reject`);
            notify("Rejected.");
            return;
          }
          await del(
            `${P(ctx)}/memory/agent/${encodeURIComponent(m.agent)}/${m.raw.id}` +
            `?layer=${encodeURIComponent(m.layer)}`);
          notify("Deleted.");
        } },
    ],
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
    actions: [
      { label: "Enable", run: async (j) => {
        await post(`${P(ctx)}/cron/${j.id}/enable`); notify("Enabled.");
      } },
      { label: "Disable", run: async (j) => {
        await post(`${P(ctx)}/cron/${j.id}/disable`); notify("Disabled.");
      } },
      { label: "Run now", run: async (j) => {
        await post(`${P(ctx)}/cron/${j.id}/run-now`);
        notify("Queued a run."); return false;
      } },
      { label: "Remove", danger: true,
        confirm: (j) => `Remove cron job '${j.name}'?`,
        run: async (j) => { await del(`${P(ctx)}/cron/${j.id}`); notify("Removed."); } },
      { label: "Refresh", needsSelection: false, run: async () => {} },
    ],
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
    actions: [
      { label: "Update docs", needsSelection: false, run: async () => {
        const x = await post("/docs/update");
        notify(x.status);
      } },
      { label: "Open online", run: async () => {
        window.open("https://modulatio.ai/", "_blank", "noopener");
        return false;
      } },
    ],
    renderDetail: async (d) => {
      const page_ = await api(`/docs/${d.slug}`);
      return [heading(d.title), pre(page_.markdown)];
    },
  });
}
