// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
// The one fetch wrapper. Bearer pairing token (LAN binds) rides every
// /api call from localStorage; loopback needs none.

const TOKEN_KEY = "modulatio-webos-token";

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

export async function api(path, { method = "GET", body } = {}) {
  const resp = await fetch(`/api${path}`, {
    method,
    headers: {
      // A custom header a cross-origin "simple request" can't set — the
      // CSRF guard requires it on every state-changing call.
      "X-Modulatio-WebOS": "1",
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
