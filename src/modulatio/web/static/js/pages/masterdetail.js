// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
// The MasterDetail archetype — built once, nine pages are bindings.
// List pane: heading + count badge + search, borderless rows in one
// card, selected row inverts. Detail pane: a pure function of the
// selected item. (design §1.2)

import { el } from "../dom.js";

export function masterDetail(page, cfg) {
  const {
    title, load, columns, rowLabel, renderDetail,
    wideDetail = false, emptyText = "nothing here yet",
  } = cfg;

  const badge = el("span", { class: "soft", style: "font-weight:400" }, "…");
  const search = el("input", {
    class: "mono search", type: "search", placeholder: "search",
    "aria-label": `search ${title}`,
  });
  const tbody = el("tbody", {});
  const listPane = el("section", { class: "card" },
    el("div", { class: "spread", style: "margin-bottom:10px" },
      el("h2", { style: "margin-bottom:0" }, `${title} `, badge), search),
    el("div", { style: "overflow-x:auto" },
      el("table", { class: "rows" },
        el("thead", {}, el("tr", {}, ...columns.map((c) => el("th", {}, c.label)))),
        tbody)),
  );
  const detailPane = el("section", { class: "card detail" },
    el("p", { class: "soft" }, "select a row"));
  page.append(el("div", {
    class: wideDetail ? "masterdetail masterdetail--wide-detail" : "masterdetail",
  }, listPane, detailPane));

  let items = [];
  let selected = null;

  function renderRows() {
    const q = search.value.trim().toLowerCase();
    const visible = q
      ? items.filter((it) => rowLabel(it).toLowerCase().includes(q))
      : items;
    badge.textContent = `· ${visible.length}`;
    tbody.replaceChildren(...visible.map((it) => {
      const tr = el("tr", {
        "aria-selected": String(it === selected),
        onclick: () => select(it, tr),
      }, ...columns.map((c) => el("td", c.mono ? { class: "mono" } : {}, c.cell(it))));
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

  search.addEventListener("input", renderRows);

  (async () => {
    try {
      items = await load();
      renderRows();
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
