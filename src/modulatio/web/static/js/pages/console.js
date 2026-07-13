// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
// The Console — the TUI's centerpiece in the browser. Lamp row,
// LEADER / MOD SQUAD flip (F4), the TV fed by the SSE lane, the run
// telemetry rail, the composer (`/kickoff … /end` brackets are the ONLY
// job trigger), and the fail-closed approval modal.

import { api, ApiError } from "../api.js";
import { el } from "../dom.js";
import { subscribe } from "../events.js";

// ── the TUI's lane predicates, ported verbatim ─────────────────────

const RUN_LEVEL_ROLES = new Set(["orchestrator", "comptroller"]);

function isLeaderRole(role) {
  return role === "leader" || role === "planner" || (role ?? "").startsWith("leader-");
}

function isTeamRole(role) {
  return Boolean(role) && !isLeaderRole(role) && !RUN_LEVEL_ROLES.has(role);
}

// ── the TUI's glyph + verb vocabulary, ported verbatim ─────────────

const PHASE = {
  kickoff_started: ["▸▸", "kicked off the run"],
  kickoff_ended: ["■■", "run complete"],
  leader_decompose_started: ["◆◆", "is decomposing the objective"],
  leader_decompose_ended: ["◆◆", "decomposed it into goals"],
  leader_verify_started: ["◇◇", "is verifying the goal"],
  leader_verify_ended: ["✓✓", "rendered a verdict"],
  task_dispatched: ["▸▸", "picked up a task"],
  qc_started: ["○○", "is reviewing"],
  qc_verdict: ["✓✓", "returned a QC verdict"],
  task_completed: ["✓✓", "finished a task"],
  task_settled: ["■■", "wrapped up a task"],
  ticket_opened: ["!!", "opened a ticket"],
  scope_drift_warning: ["!!", "flagged scope drift"],
  leader_thinking: ["◆◆", "is thinking"],
  leader_answered: ["◆◆", "answered"],
  task_planning_started: ["◆◆", "is planning the work"],
  task_planning_ended: ["◆◆", "planned the work"],
  qc_review: ["○○", "is reviewing"],
  qc_authored_fix: ["✚✚", "authored the fix"],
  qc_fix_failed: ["!!", "couldn't author a fix"],
  task_decomposed: ["»»", "split the task"],
  model_fallback: ["◄►", "fell back to a backup model"],
  skill_codified: ["★★", "codified a skill"],
  jt_codified: ["★★", "codified a job template"],
  leader_self_fix: ["✚✚", "fixed it directly"],
  dispatch_aborted: ["!!", "attempt stopped by the breaker"],
};

const TOOL = {
  web_search: ["◉◉", "is searching the web"],
  http_get: ["▼▼", "is reading a page"],
  write_artifact: ["✎✎", "is writing"],
  edit_file: ["✎✎", "is revising"],
  read_file: ["□□", "is reading"],
  run_shell: ["▲▲", "is building"],
  search_skills: ["★★", "is browsing skills"],
  load_skill: ["★★", "picked up a skill"],
  drop_skill: ["★★", "set a skill down"],
  read_tool_result: ["□□", "is re-reading a result"],
};

function humanize(token) {
  const t = (token ?? "").replaceAll("-", " ").replaceAll("_", " ").trim();
  return t ? t.replace(/\b\w/g, (c) => c.toUpperCase()) : token;
}

function glyphVerb(ev) {
  if (ev.phase === "tool_call_ended") {
    const tool = typeof ev.detail === "object" ? ev.detail?.tool : "";
    if (!tool) return ["●●", "ran a tool"];
    return TOOL[tool] ?? ["●●", `ran ${humanize(tool).toLowerCase()}`];
  }
  return PHASE[ev.phase] ?? ["··", ev.phase];
}

const MAX_LINES = 2000; // the TUI's prune

// ── page ───────────────────────────────────────────────────────────

export function mountConsole(page, ctx) {
  if (!ctx.project) {
    page.append(el("section", { class: "card", style: "max-width:520px" },
      el("h2", {}, "Console"),
      el("p", { class: "soft" },
        "No project yet — create one in the terminal (`modulatio`) or the CONFIG tab."),
    ));
    return;
  }

  const state = {
    lane: "leader",
    mode: "default", // the converse Leader's autonomy mode (/yolo //goal …)
    running: false,
    producers: new Map(), // agent_id → {name, glyph, verb}
    tokens: 0,
    elapsed: 0,
    tickets: 0,
    liveRun: null,
    telemetry: null, // last frame — the gauges land, they don't reset
  };

  // — lamps —
  const lamps = el("div", { id: "lamps", class: "mono soft" });

  // — flip —
  const btnLeader = el("button", { class: "tab", onclick: () => flip("leader") }, "LEADER");
  const btnTeam = el("button", { class: "tab", onclick: () => flip("team") }, "MOD SQUAD");
  const btnClear = el("button", {
    class: "tab", title: "Clear the activity log (both lanes)",
    onclick: () => clearScreen(),
  }, "CLEAR");

  // — TVs —
  const tvLeader = el("div", { class: "card tv", role: "log", "aria-label": "Leader lane" });
  const tvTeam = el("div", { class: "card tv", role: "log", "aria-label": "Team lane" });
  const rail = el("aside", { class: "card rail mono" });
  const teamWrap = el("div", { class: "teamwrap", hidden: "" }, rail, tvTeam);

  const statusLine = el("div", { class: "soft mono statusline" }, "standby");

  // — composer —
  const input = el("textarea", {
    class: "composer-input mono",
    rows: "3",
    placeholder: "Speak with the Leader — or bracket a job:  /kickoff …objective… /end",
    "aria-label": "Composer",
  });
  const btnSend = el("button", { class: "btn btn--primary", onclick: send }, "Send");
  const btnKick = el("button", {
    class: "btn", title: "Bracket what you typed as a job and kick it off",
    onclick: () => {
      const raw = input.value.trim();
      if (!raw) {
        // Nothing to kick off yet — drop in the brackets and let them type.
        input.value = "/kickoff \n\n/end";
        input.focus();
        input.setSelectionRange(9, 9);
        return;
      }
      // Bare objective → bracket it; already-bracketed → leave as is; then GO.
      if (!/^\s*\/kickoff/i.test(raw)) input.value = `/kickoff ${raw} /end`;
      send();
    },
  }, "Kick off…");
  const btnStop = el("button", {
    class: "btn", disabled: "", onclick: stopRun,
  }, "⏹ Stop (F8)");
  const composer = el("div", { class: "card composer" },
    input,
    el("div", { class: "spread" },
      el("span", { class: "soft", style: "font-size:12.5px" },
        "Enter sends · Shift+Enter newline · /kickoff … /end starts the job"),
      el("div", { class: "row" }, btnKick, btnStop, btnSend)),
  );

  // — approval modal —
  const modal = el("dialog", { class: "approval card" });

  const consoleSection = el("section", { class: "stack console", "data-lane": "leader" },
    el("div", { class: "spread" }, lamps,
      el("div", { class: "row" }, btnLeader, btnTeam, btnClear)),
    tvLeader, teamWrap, statusLine, composer);
  page.append(consoleSection, modal);

  // ── rendering ────────────────────────────────────────────────────

  function lamp(glyph, word, { attention = false } = {}) {
    return el("span", { class: attention ? "lamp attention" : "lamp" },
      el("span", { class: "glyph" }, glyph), word);
  }

  function renderLamps() {
    const mm = String(Math.floor(state.elapsed / 60)).padStart(2, "0");
    const ss = String(Math.floor(state.elapsed % 60)).padStart(2, "0");
    lamps.replaceChildren(
      lamp("●", "leader"), " · ",
      // The autonomy pill — the converse Leader's mode; attention when the
      // operator has handed anything over (any non-default mode).
      lamp(state.mode === "default" ? "◌" : "◉", state.mode,
        { attention: state.mode !== "default" }), " · ",
      lamp("◇", `${state.producers.size} mods`), " · ",
      state.running ? lamp("▸", "running", { attention: true }) : lamp("▸", "idle"),
      " · ",
      lamp("⚑", `${state.tickets} tickets`, { attention: state.tickets > 0 }), " · ",
      lamp("⛁", `${fmtTokens(state.tokens)} tok`), " · ",
      lamp("◷", `${mm}:${ss}`),
    );
  }

  function fmtTokens(n) {
    return n >= 1000 ? `${(n / 1000).toFixed(1)}K` : String(n);
  }

  function flip(lane) {
    state.lane = lane;
    tvLeader.hidden = lane !== "leader";
    teamWrap.hidden = lane !== "team";
    btnLeader.setAttribute("aria-current", lane === "leader" ? "page" : "false");
    btnTeam.setAttribute("aria-current", lane === "team" ? "page" : "false");
    // The composer talks to the Leader — no kickoff box on the team view.
    composer.hidden = lane !== "leader";
    // Team view has no composer, so the TV grows to fill (CSS on the lane).
    consoleSection.dataset.lane = lane;
    // A hidden lane can't scroll while hidden — catch it up now that it shows.
    const shown = lane === "leader" ? tvLeader : tvTeam;
    shown.scrollTop = shown.scrollHeight;
  }

  // Rebuild-on-(re)connect: the server replays the current run on every
  // connection, so clear the view and let the replay repaint it — no duplicate
  // lines, and a reconnect or tab-return restores the whole stream.
  function resetView() {
    tvLeader.replaceChildren();
    tvTeam.replaceChildren();
    state.producers.clear();
    state.telemetry = null;
    renderRail();
  }

  // Operator "clear screen" — wipe both activity logs AND every repaint
  // source, so neither a reconnect nor a tab-return brings the cleared log
  // back: the server replay buffer is dropped, and
  // the conversation history — which the boot paint replays in full — gets a
  // turn-count watermark so remounts skip what was cleared. The thread itself
  // stays intact (reset is the destructive verb); telemetry and any live run
  // are untouched.
  const clearMarkKey = `modulatio.clearmark.${ctx.project}`;
  function clearScreen() {
    tvLeader.replaceChildren();
    tvTeam.replaceChildren();
    api(`/${ctx.project}/console/clear`, { method: "POST" }).catch(() => {});
    api(`/${ctx.project}/conversation`)
      .then(({ turns }) => localStorage.setItem(clearMarkKey, String(turns.length)))
      .catch(() => {});
  }

  function appendLine(tv, node) {
    tv.append(node);
    while (tv.childElementCount > MAX_LINES) tv.firstElementChild.remove();
    tv.scrollTop = tv.scrollHeight;
  }

  function operatorLine(text) {
    // The accent marker alone distinguishes the operator's lines — no "you"
    // label (the operator knows who they are). Mirrors the TUI's rendering.
    appendLine(tvLeader, el("div", { class: "stream-line op mono" }, `▸ ${text}`));
  }

  function leaderSpeech(text) {
    appendLine(tvLeader, el("div", { class: "leader-speech" }, text));
  }

  function eventLine(ev) {
    const [glyph, verb] = glyphVerb(ev);
    const name = humanize(ev.agent_id || ev.role);
    const task = ev.task_id ? `  ·  ${ev.task_id}` : "";
    const line = el("div", { class: "stream-line mono" },
      el("span", { class: "glyph" }, glyph), ` ${name} ${verb}${task}`);
    if (isLeaderRole(ev.role)) appendLine(tvLeader, line);
    else if (isTeamRole(ev.role)) {
      appendLine(tvTeam, line);
      state.producers.set(ev.agent_id, { name, glyph, verb });
      renderRail();
    } else {
      // run-level (orchestrator/comptroller): both lanes deserve the beat
      appendLine(tvLeader, line);
      appendLine(tvTeam, line.cloneNode(true));
    }
    if (ev.phase === "kickoff_started") setRunning(true);
    if (ev.phase === "kickoff_ended") setRunning(false);
    if (ev.phase === "ticket_opened") { state.tickets += 1; renderLamps(); }
  }

  function setRunning(running) {
    state.running = running;
    if (!running) state.producers.clear();
    btnStop.toggleAttribute("disabled", !running);
    statusLine.textContent = running ? "◌ working…" : "standby";
    statusLine.classList.toggle("idle-region", !running);
    renderLamps();
    renderRail();
  }

  function renderRail() {
    const t = state.telemetry;
    const seg = (pct) => "▰".repeat(Math.round(pct / 10)).padEnd(10, "▱");
    const rows = [
      el("h2", {}, "Run telemetry"),
      el("div", {}, `⏱ ${fmtElapsed(state.elapsed)}`),
    ];
    if (t) {
      rows.push(
        el("div", {}, `tasks ${seg(t.pct)} ${t.pct}%`),
        el("div", {}, `qc    ✗ ${t.qc_rejected} rejected`),
        el("div", {}, `tok   ↑ ${fmtTokens(t.tokens_in || 0)} in · ↓ ${fmtTokens(t.tokens_out || 0)} out`),
        el("div", {}, `ctx   ${fmtTokens(t.tokens)} tok · ${t.compressions} compress`),
      );
    } else {
      rows.push(el("div", { class: "soft" }, "tasks ▱▱▱▱▱▱▱▱▱▱ —"));
    }
    rows.push(el("div", { class: "soft", style: "margin-top:8px" }, "── on the floor ──"));
    if (state.producers.size === 0) {
      rows.push(el("div", { class: "soft" }, "(idle)"));
    } else {
      for (const p of state.producers.values()) {
        rows.push(el("div", {}, `◆◆ ${p.name}  ${p.glyph} ${p.verb}`));
      }
    }
    rail.replaceChildren(...rows);
  }

  function fmtElapsed(s) {
    const mm = String(Math.floor(s / 60)).padStart(2, "0");
    return `${mm}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  }

  // ── approval modal (fail-closed: Esc/close = deny) ───────────────

  // The TUI modal's scope vocabulary, verbatim — buttons render only for the
  // scopes THIS request offers (available_scopes), so the browser can never
  // present a grant the gate wouldn't accept.
  const SCOPE_LABELS = {
    once: "Once", session: "This session", always: "Always", deny: "✗ Deny",
  };

  function showApproval(data) {
    const scopes = (data.available_scopes || ["once", "deny"])
      .filter((s) => s in SCOPE_LABELS);
    // Deny leads, Always sits rightmost — mirror the risk gradient.
    const ordered = ["deny", "once", "session", "always"].filter((s) => scopes.includes(s));
    const capLine = data.cap_value != null
      ? el("p", { class: "mono soft" }, `cap: ${data.cap_value} ${data.cap_unit || ""}`)
      : "";
    modal.replaceChildren(
      el("h2", {}, "Leader asks permission"),
      el("p", { class: "mono" }, `${data.action}  ·  ${data.resource || ""}`),
      el("p", {}, data.why || ""),
      capLine,
      el("div", { class: "row", style: "justify-content:flex-end; flex-wrap:wrap" },
        ...ordered.map((s) => el("button", {
          class: s === "deny" ? "btn btn--danger"
            : s === "always" ? "btn btn--primary" : "btn",
          onclick: () => decide(data.id, s),
        }, SCOPE_LABELS[s]))),
    );
    modal.showModal();
  }

  async function decide(rid, scope) {
    modal.close();
    try {
      await api(`/${ctx.project}/approvals/${rid}`, { method: "POST", body: { scope } });
    } catch {
      /* already resolved / timed out server-side — fail-closed either way */
    }
  }

  modal.addEventListener("cancel", () => {
    // Esc — the deny is the server timeout's job too, but be explicit.
    const rid = modal.dataset.rid;
    if (rid) decide(rid, "deny");
  });

  // ── frame routing ────────────────────────────────────────────────

  const unsubscribe = subscribe(ctx.project, (frame) => {
    if (frame.type === "event") eventLine(frame.data);
    else if (frame.type === "telemetry") {
      // Telemetry only flows during a run — so it can't be "standby". Belt over
      // run_started (which the replay now delivers) so the status never lies.
      if (!state.running) setRunning(true);
      state.telemetry = frame.data;
      state.tokens = frame.data.tokens;
      state.elapsed = frame.data.elapsed_s;
      renderLamps();
      renderRail();
    } else if (frame.type === "run_started") {
      state.liveRun = frame.data.run_id;
      setRunning(true);
    } else if (frame.type === "run_done") {
      setRunning(false);
      const note = frame.data.error
        ? `✗ run ${frame.data.run_id} failed — ${frame.data.error}`
        : `■■ ${frame.data.run_id} — ${frame.data.digest || "run complete"}`;
      appendLine(tvLeader, el("div", { class: "stream-line mono" }, note));
      appendLine(tvTeam, el("div", { class: "stream-line mono" }, note));
    } else if (frame.type === "mode") {
      state.mode = frame.data.mode || "default";
      renderLamps();
    } else if (frame.type === "approval_request") {
      modal.dataset.rid = frame.data.id;
      showApproval(frame.data);
    }
  }, {
    onStatus: (s) => {
      if (s === "connected") resetView();
      else if (s === "reconnecting") statusLine.textContent = "◌ reconnecting…";
    },
  });

  // ── composer ─────────────────────────────────────────────────────

  const KICKOFF_RE = /^\s*\/kickoff\b([\s\S]*?)\/end\s*$/i;

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    const m = text.match(KICKOFF_RE);
    if (m) {
      const objective = m[1].trim();
      if (!objective) { statusLine.textContent = "✗ empty objective"; return; }
      operatorLine(`/kickoff ${objective} /end`);
      try {
        const { run_id } = await api(`/${ctx.project}/kickoff`, {
          method: "POST", body: { objective },
        });
        state.liveRun = run_id;
      } catch (err) {
        statusLine.textContent = err instanceof ApiError && err.status === 409
          ? `✗ a run is already in flight (${err.detail?.run_id ?? "…"})`
          : `✗ kickoff failed — ${err.message}`;
      }
      return;
    }
    operatorLine(text);
    statusLine.textContent = "◌ the Leader is thinking…";
    try {
      const { reply } = await api(`/${ctx.project}/converse`, {
        method: "POST", body: { text },
      });
      leaderSpeech(reply);
    } catch (err) {
      appendLine(tvLeader, el("div", { class: "stream-line mono error-text" },
        `converse failed — ${err.message}`));
    } finally {
      if (!state.running) statusLine.textContent = "standby";
    }
  }

  async function stopRun() {
    if (!state.running) return;
    if (!confirm("Stop the running job? (the web F8)")) return;
    await api(`/${ctx.project}/stop`, { method: "POST" });
  }

  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); send(); }
  });

  function onKey(ev) {
    if (ev.key === "F4") {
      ev.preventDefault();
      flip(state.lane === "leader" ? "team" : "leader");
    } else if (ev.key === "F8") {
      ev.preventDefault();
      stopRun();
    }
  }
  window.addEventListener("keydown", onKey);

  // ── boot: history, tickets, first paint ──────────────────────────

  (async () => {
    try {
      const { turns } = await api(`/${ctx.project}/conversation`);
      // Honor the operator's clear: skip the watermarked prefix. A watermark
      // LONGER than the thread means the thread was reset since — drop it.
      let skip = parseInt(localStorage.getItem(clearMarkKey) || "0", 10) || 0;
      if (skip > turns.length) { skip = 0; localStorage.removeItem(clearMarkKey); }
      for (const t of turns.slice(skip)) {
        if (t.role === "operator") operatorLine(t.content);
        else leaderSpeech(t.content);
      }
    } catch { /* fresh project — no thread yet */ }
    try {
      const { tickets } = await api(`/${ctx.project}/tickets`);
      state.tickets = tickets.filter((t) => t.status === "open").length;
    } catch { /* lamp stays 0 */ }
    try {
      const { mode } = await api(`/${ctx.project}/mode`);
      state.mode = mode || "default";
    } catch { /* pill stays default */ }
    renderLamps();
    renderRail();
    flip("leader");
  })();

  return () => {
    unsubscribe();
    window.removeEventListener("keydown", onKey);
  };
}
