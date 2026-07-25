// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
// The MasterDetail archetype — built once, nine pages are bindings.
// List pane: heading + count badge + search, borderless rows in one
// card, selected row inverts. Detail pane: a pure function of the
// selected item.

import { el } from "../dom.js";

export function masterDetail(page, cfg) {
  const {
    title, load, columns, rowLabel, renderDetail,
    wideDetail = false, emptyText = "nothing here yet", actions = [],
  } = cfg;

  const badge = el("span", { class: "soft", style: "font-weight:400" }, "…");
  const search = el("input", {
    class: "mono search", type: "search", placeholder: "search",
    "aria-label": `search ${title}`,
  });
  const tbody = el("tbody", {});
  // The action row — buttons that act on the selected item (the web's
  // clickable version of the TUI's key-actions). Rendered only when a page
  // declares actions.
  const actionButtons = actions.map((a) => {
    const btn = el("button", {
      class: a.danger ? "btn btn--danger" : "btn",
      onclick: () => runAction(a, btn),
    }, a.label);
    return { spec: a, btn };
  });
  const actionRow = actionButtons.length
    ? el("div", { class: "row action-row" }, ...actionButtons.map((a) => a.btn))
    : null;
  const listPane = el("section", { class: "card" },
    el("div", { class: "spread", style: "margin-bottom:10px" },
      el("h2", { style: "margin-bottom:0" }, `${title} `, badge), search),
    el("div", { style: "overflow-x:auto" },
      el("table", { class: "rows" },
        el("thead", {}, el("tr", {}, ...columns.map((c) => el("th", {}, c.label)))),
        tbody)),
    ...(actionRow ? [actionRow] : []),
  );
  const detailPane = el("section", { class: "card detail" },
    el("p", { class: "soft" }, "select a row"));
  page.append(el("div", {
    class: wideDetail ? "masterdetail masterdetail--wide-detail" : "masterdetail",
  }, listPane, detailPane));

  let items = [];
  let selected = null;

  function syncActionButtons() {
    for (const { spec, btn } of actionButtons) {
      btn.disabled = spec.needsSelection !== false && selected == null;
    }
  }

  async function runAction(spec, btn) {
    if (spec.confirm) {
      const msg = typeof spec.confirm === "function"
        ? spec.confirm(selected) : spec.confirm;
      if (!(await confirmDialog(msg))) return;
    }
    btn.disabled = true;
    try {
      const keep = await spec.run(selected, { reload, notify });
      if (keep !== false) await reload();
    } catch (err) {
      notify(`${spec.label} failed — ${err.message}`, { error: true });
    } finally {
      syncActionButtons();
    }
  }

  const rowFor = new Map();  // item → its <tr>, rebuilt each render

  function renderRows() {
    const q = search.value.trim().toLowerCase();
    const visible = q
      ? items.filter((it) => rowLabel(it).toLowerCase().includes(q))
      : items;
    badge.textContent = `· ${visible.length}`;
    rowFor.clear();
    tbody.replaceChildren(...visible.map((it) => {
      const tr = el("tr", {
        "aria-selected": String(it === selected),
        onclick: () => select(it, tr),
      }, ...columns.map((c) => el("td", c.mono ? { class: "mono" } : {}, c.cell(it))));
      rowFor.set(it, tr);
      return tr;
    }));
    if (visible.length === 0) {
      tbody.append(el("tr", {}, el("td", {
        class: "soft idle-region", colspan: String(columns.length),
        style: "padding:24px; text-align:center",
      }, emptyText)));
    }
  }

  async function select(item, tr) {
    selected = item;
    syncActionButtons();
    for (const row of tbody.children) row.setAttribute("aria-selected", "false");
    tr?.setAttribute("aria-selected", "true");
    detailPane.replaceChildren(el("p", { class: "soft" }, "…"));
    try {
      const nodes = await renderDetail(item);
      detailPane.replaceChildren(...[].concat(nodes));
    } catch (err) {
      detailPane.replaceChildren(
        el("p", { class: "error-text" }, `detail failed — ${err.message}`));
    }
  }

  async function reload() {
    // Re-fetch and re-render, keeping the selection on the same row where it
    // still exists (by rowLabel), so an action's effect shows in place.
    const priorLabel = selected != null ? rowLabel(selected) : null;
    items = await load();
    const keep = priorLabel != null
      ? items.find((it) => rowLabel(it) === priorLabel) : undefined;
    selected = null;
    renderRows();
    if (keep !== undefined) {
      select(keep, rowFor.get(keep));
    } else {
      syncActionButtons();
      detailPane.replaceChildren(el("p", { class: "soft" }, "select a row"));
    }
  }

  search.addEventListener("input", renderRows);

  (async () => {
    try {
      items = await load();
      renderRows();
      syncActionButtons();
      if (items.length) select(items[0], tbody.firstElementChild);
    } catch (err) {
      badge.textContent = "";
      tbody.replaceChildren(el("tr", {}, el("td", {
        class: "error-text", colspan: String(columns.length),
      }, `load failed — ${err.message}`)));
    }
  })();
}

// Shared detail builders ─────────────────────────────────────────────

export function kv(pairs) {
  return el("dl", { class: "kv" },
    ...pairs.flatMap(([k, v]) => [
      el("dt", { class: "soft" }, k),
      el("dd", { class: "mono" }, String(v ?? "—")),
    ]));
}

export function pre(text) {
  return el("pre", { class: "mono detail-body" }, text ?? "");
}

export function heading(text) {
  return el("h2", {}, text);
}

// Dialogs + toast ─────────────────────────────────────────────────────

// A themed confirm — resolves true on OK, false on Cancel/Esc (fail-safe:
// a dismissed destructive prompt never acts). Optional labels name the
// buttons for what they DO — "Cancel" beside a question about cancelling
// something reads as ambiguous.
export function confirmDialog(message, { okLabel = "OK", cancelLabel = "Cancel" } = {}) {
  return new Promise((resolve) => {
    let decided = false;
    const done = (ok) => { decided = true; dlg.close(); resolve(ok); };
    const dlg = el("dialog", { class: "approval card" },
      el("p", { class: "approval-detail" }, message),
      el("div", { class: "row", style: "justify-content:flex-end;gap:10px" },
        el("button", { class: "btn", onclick: () => done(false) }, cancelLabel),
        el("button", { class: "btn btn--danger", onclick: () => done(true) }, okLabel)));
    dlg.addEventListener("close", () => {
      if (!decided) resolve(false);
      dlg.remove();
    });
    document.body.append(dlg);
    dlg.showModal();
  });
}

// A themed form — fields: [{name, label, type?, value?, textarea?}].
// Resolves the values object on Save, null on Cancel/Esc.
export function formDialog(title, fields) {
  return new Promise((resolve) => {
    let saved = false;
    const inputs = fields.map((f) => {
      let node;
      if (f.options) {
        // Options are plain strings, or {value, label} when the display text
        // differs from the submitted value (e.g. a free-flagged model id).
        node = el("select", { class: "mono form-input" },
          ...f.options.map((o) => el("option", { value: o.value ?? o }, o.label ?? o)));
      } else if (f.textarea) {
        node = el("textarea", { class: "mono form-input", rows: "5" });
      } else {
        node = el("input", { class: "mono form-input", type: f.type || "text" });
      }
      if (f.value != null) node.value = f.value;
      return { f, node };
    });
    const submit = () => {
      const out = {};
      for (const { f, node } of inputs) out[f.name] = node.value;
      saved = true; dlg.close(); resolve(out);
    };
    const dlg = el("dialog", { class: "approval card" },
      el("h2", {}, title),
      ...inputs.map(({ f, node }) =>
        el("label", { class: "form-field" },
          el("span", { class: "soft" }, f.label), node)),
      el("div", { class: "row", style: "justify-content:flex-end;gap:10px" },
        el("button", { class: "btn", onclick: () => dlg.close() }, "Cancel"),
        el("button", { class: "btn btn--primary", onclick: submit }, "Save")));
    dlg.addEventListener("close", () => {
      if (!saved) resolve(null);
      dlg.remove();
    });
    document.body.append(dlg);
    dlg.showModal();
    inputs[0]?.node.focus();
  });
}

// A transient status toast.
let _toast;
export function notify(message, { error = false } = {}) {
  if (!_toast) {
    _toast = el("div", { class: "toast", role: "status" });
    document.body.append(_toast);
  }
  _toast.textContent = message;
  _toast.className = error ? "toast toast--error show" : "toast show";
  clearTimeout(notify._t);
  notify._t = setTimeout(() => { _toast.className = _toast.className.replace(" show", ""); }, 4000);
}
