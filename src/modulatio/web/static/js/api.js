// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
// The one fetch wrapper. Bearer pairing token (LAN binds) rides every
// /api call from localStorage; loopback needs none.

const TOKEN_KEY = "modulatio-webos-token";

//: This tab's identity for staged uploads. Two browsers open on one project
//: are two people: a turn finishing in one must not discard what the other has
//: attached and not yet sent.
let _composer = "";

export function composerId() {
  if (!_composer) {
    _composer = (crypto.randomUUID?.() ?? String(Math.random())).slice(0, 32);
  }
  return _composer;
}

export function authHeaders() {
  const token = localStorage.getItem(TOKEN_KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

// Raw bytes rather than JSON: encoding a file as JSON would inflate it and
// force the whole thing through a string on both sides. The filename travels
// in a header as a LABEL — the server treats it as one, never as a location.
export async function apiUpload(path, file) {
  const resp = await fetch(`/api${path}`, {
    method: "POST",
    headers: {
      "X-Modulatio-WebOS": "1",
      ...authHeaders(),
      "Content-Type": "application/octet-stream",
      "X-Modulatio-Filename": encodeURIComponent(file.name || "upload"),
      "X-Modulatio-Composer": composerId(),
    },
    body: file,
  });
  let payload = null;
  try {
    payload = await resp.json();
  } catch {
    /* non-JSON error body — status carries the story */
  }
  if (!resp.ok) throw new ApiError(resp.status, payload?.detail ?? resp.statusText);
  return payload;
}

export async function api(path, { method = "GET", body } = {}) {
  const resp = await fetch(`/api${path}`, {
    method,
    headers: {
      // A custom header a cross-origin "simple request" can't set — the
      // CSRF guard requires it on every state-changing call.
      "X-Modulatio-WebOS": "1",
      "X-Modulatio-Composer": composerId(),
      ...authHeaders(),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let payload = null;
  try {
    payload = await resp.json();
  } catch {
    /* non-JSON error body — status carries the story */
  }
  if (!resp.ok) throw new ApiError(resp.status, payload?.detail ?? resp.statusText);
  return payload;
}
