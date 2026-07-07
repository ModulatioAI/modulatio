// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
// SSE over fetch-streaming. Not EventSource: it can't carry the bearer
// header, and fetch gives us the same wire with uniform auth. Frames
// arrive as {type, data}; the connection self-heals with backoff.

import { authHeaders } from "./api.js";

const RECONNECT_MS = 2000;

export function subscribe(project, onFrame, { onStatus } = {}) {
  const ctrl = new AbortController();

  (async () => {
    while (!ctrl.signal.aborted) {
      try {
        const resp = await fetch(`/api/${project}/events`, {
          signal: ctrl.signal,
          headers: authHeaders(),
        });
        if (!resp.ok) throw new Error(`events: HTTP ${resp.status}`);
        onStatus?.("connected");
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let sep;
          while ((sep = buf.indexOf("\n\n")) >= 0) {
            const raw = buf.slice(0, sep);
            buf = buf.slice(sep + 2);
            const frame = parseFrame(raw);
            if (frame) onFrame(frame);
          }
        }
      } catch {
        if (ctrl.signal.aborted) return;
      }
      onStatus?.("reconnecting");
      await new Promise((r) => setTimeout(r, RECONNECT_MS));
    }
  })();

  return () => ctrl.abort();
}

function parseFrame(raw) {
  let type = "event";
  let data = null;
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) {
      try {
        data = JSON.parse(line.slice(5));
      } catch {
        return null;
      }
    }
    // lines starting ":" are keepalive comments — skipped by not matching
  }
  return data === null ? null : { type, data };
}
