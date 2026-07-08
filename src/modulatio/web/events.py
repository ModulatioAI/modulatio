# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""The per-project event bus behind the SSE stream.

Publishers are engine worker threads (the actor's ``activity_callback``);
subscribers are async SSE generators. ``queue.Queue`` is the bridge —
thread-safe on the publish side, drained with ``asyncio.to_thread`` on
the async side — so no event-loop bookkeeping leaks into the engine
path. No replay: a subscriber sees only what happens after it connects
(history lives in the vault, not the bus).
"""

from __future__ import annotations

import queue
import threading
from collections import deque

#: Frames a subscriber can miss before we drop the oldest — a stuck
#: browser must never wedge the publishing engine thread.
_SUBSCRIBER_DEPTH = 1000

#: The current run's activity frames held for replay, so a (re)connecting
#: browser rebuilds the whole stream instead of only what happens next. The
#: SSE self-heals every 2s and a tab switch re-subscribes; without replay any
#: event in the gap is lost (leader decompose, producer bursts). Telemetry is
#: kept separately (latest-only) — it fires 1/s and is idempotent, so buffering
#: every frame would evict the real events.
_REPLAY_DEPTH = 4000


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._replay: deque[dict] = deque(maxlen=_REPLAY_DEPTH)
        self._last_telemetry: dict | None = None

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=_SUBSCRIBER_DEPTH)
        with self._lock:
            # Replay the current run first, then register for live frames —
            # both under the lock so no live frame slips in between.
            for frame in self._replay:
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    break
            if self._last_telemetry is not None:
                try:
                    q.put_nowait(self._last_telemetry)
                except queue.Full:
                    pass
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, frame: dict) -> None:
        with self._lock:
            # Keep the run's stream for replay; a new run_started resets it.
            ftype = frame.get("type")
            if ftype == "run_started":
                self._replay.clear()
                self._last_telemetry = None
                self._replay.append(frame)
            elif ftype == "telemetry":
                self._last_telemetry = frame
            else:
                self._replay.append(frame)
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(frame)
            except queue.Full:
                # Slow consumer: shed the oldest frame, keep the newest.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    pass


_buses: dict[str, EventBus] = {}
_buses_lock = threading.Lock()


def get_bus(project_code: str) -> EventBus:
    with _buses_lock:
        bus = _buses.get(project_code)
        if bus is None:
            bus = _buses[project_code] = EventBus()
        return bus
