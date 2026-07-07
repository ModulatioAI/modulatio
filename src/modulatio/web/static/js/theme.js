// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
// The Feng-Web theme state. Two themes, their per-theme options, and
// the F2 cycle — persisted per-browser; server-side preference sync
// arrives with the settings page.

const KEY = "modulatio-webos-theme";

export const ATELIER_FIELDS = ["sage", "reed", "mist", "clay", "heather", "bone"];

const DEFAULTS = {
  theme: "atelier",
  field: "sage",       // atelier's operator field
  vellum: "standard",  // or "inverted"
  vfield: "sage",      // vellum's paper: sage | grey
};

let state = { ...DEFAULTS };

export function loadTheme() {
  try {
    state = { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) ?? "{}") };
  } catch {
    state = { ...DEFAULTS };
  }
  apply();
  return state;
}

export function themeState() {
  return { ...state };
}

export function themeLabel() {
  return state.theme === "atelier"
    ? `atelier · ${state.field}`
    : `vellum · ${state.vellum}${state.vfield === "grey" ? " · grey" : ""}`;
}

export function setTheme(patch) {
  state = { ...state, ...patch };
  localStorage.setItem(KEY, JSON.stringify(state));
  apply();
}

export function cycleTheme() {
  setTheme({ theme: state.theme === "atelier" ? "vellum" : "atelier" });
}

function apply() {
  const root = document.documentElement;
  root.dataset.theme = state.theme;
  root.dataset.field = state.field;
  root.dataset.vellum = state.vellum;
  root.dataset.vfield = state.vfield;
  document.dispatchEvent(new CustomEvent("themechange", { detail: themeState() }));
}
