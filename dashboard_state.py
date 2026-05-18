"""In-memory event bus for the live booking dashboard."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


class DashboardEventBus:
    """Broadcast dashboard events to connected WebSocket clients."""

    def __init__(self, history_limit: int = 250) -> None:
        self._history: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    async def publish(
        self,
        event_type: str,
        *,
        call_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": str(uuid4()),
            "type": event_type,
            "call_id": str(call_id) if call_id is not None else None,
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": payload or {},
        }

        async with self._lock:
            self._history.append(event)
            subscribers = tuple(self._subscribers)

        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

        return event

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.add(queue)
            history = tuple(self._history)

        for event in history[-queue.maxsize :]:
            queue.put_nowait(event)

        return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            return list(self._history)


dashboard_events = DashboardEventBus()


async def emit_call_started(call_id: str) -> None:
    await dashboard_events.publish("call_started", call_id=call_id)


async def emit_call_answered(call_id: str) -> None:
    await dashboard_events.publish("call_answered", call_id=call_id)


async def emit_call_ended(call_id: str) -> None:
    await dashboard_events.publish("call_ended", call_id=call_id)


async def emit_transcript(
    call_id: str, speaker: str, text: str, *, is_final: bool = True
) -> None:
    await dashboard_events.publish(
        "transcript",
        call_id=call_id,
        payload={"speaker": speaker, "text": text, "is_final": is_final},
    )


async def emit_tool_call(call_id: str, name: str, args: dict[str, Any]) -> None:
    await dashboard_events.publish(
        "tool_call",
        call_id=call_id,
        payload={"name": name, "args": args},
    )


async def emit_booking_confirmed(
    call_id: str, booking: dict[str, Any], result: dict[str, Any]
) -> None:
    await dashboard_events.publish(
        "booking_confirmed",
        call_id=call_id,
        payload={"booking": booking, "result": result},
    )


async def emit_error(call_id: str | None, message: str) -> None:
    await dashboard_events.publish("error", call_id=call_id, payload={"message": message})
