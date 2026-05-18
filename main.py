"""
XanhSM Passenger Booking Workflow — Voice AI Agent
===================================================
Bridges the Telcoflow SDK (telephony) with the Google GenAI Gemini Live API
to run a stateful taxi-booking conversation over a phone call.

State machine:
  1. Greet → identify taxi-booking intent
  2. Collect & confirm: pickup, destination, phone number, date/time
  3. make_booking  (tool call – all 4 fields required)
  4. Inform user → confirmation sent
  5. hang_up_call  (tool call – terminates session)

Patterns align with Gemini Live ↔ Telcoflow examples: structured logging,
`CallEvent.CALL_TERMINATED` cleanup, dedicated asyncio tasks with
cancel-safe shutdown, and explicit handling of Gemini / WebSocket closures.
"""

import asyncio
import logging
import os
from contextlib import suppress
from typing import Optional
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from google.genai.live import AsyncSession
from telcoflow_sdk import TelcoflowClient, TelcoflowClientConfig, ActiveCall
from telcoflow_sdk.events import CallEvent, ClientEvent
from telcoflow_sdk.exceptions import BufferFullError
from websockets import ConnectionClosed

from dashboard_state import (
    emit_booking_confirmed,
    emit_call_answered,
    emit_call_ended,
    emit_call_started,
    emit_error,
    emit_tool_call,
    emit_transcript,
)

# ---------------------------------------------------------------------------
# Configuration & logging
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def _gemini_developer_client() -> genai.Client:
    """Gemini Developer API client.

    google-genai probes GOOGLE_API_KEY and GEMINI_API_KEY and warns if both
    are set; temporarily hide GOOGLE_API_KEY when GEMINI_API_KEY is preferred.
    """
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("Set GEMINI_API_KEY or GOOGLE_API_KEY for Gemini Live.")
    google_backup = None
    if os.getenv("GEMINI_API_KEY") and os.getenv("GOOGLE_API_KEY"):
        google_backup = os.environ.pop("GOOGLE_API_KEY")
    try:
        return genai.Client(vertexai=False, api_key=key)
    finally:
        if google_backup is not None:
            os.environ["GOOGLE_API_KEY"] = google_backup


genai_client = _gemini_developer_client()
MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
SAMPLE_RATE = int(os.getenv("TELCOFLOW_SAMPLE_RATE", "24000"))
POST_BOOKING_HANG_UP_DELAY_SECONDS = float(
    os.getenv("POST_BOOKING_HANG_UP_DELAY_SECONDS", "7")
)
DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "true").lower() not in {
    "0",
    "false",
    "no",
}
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))

GREETING_KICKOFF_REALTIME_TEXT = (
    "The inbound phone call is now connected — the caller is on the line and can hear you. "
    "Speak aloud your opening greeting immediately, following your system instructions."
)

SYSTEM_INSTRUCTION = """
You are **BookBot**, the official voice-based taxi booking assistant for **XanhSM**.
You are warm, professional, concise, and always speak in a helpful tone.

## STRICT WORKFLOW — follow this exact order every call:

### Step 1 – Greeting & Intent
Greet the caller warmly. Example: "Hello! Welcome to XanhSM Taxi Services. How can I help you today?"
Identify that the customer wants to book a taxi. If their intent is unclear, gently ask how you can assist.

### Step 2 – Collect and Confirm 4 Data Points
You MUST collect AND explicitly confirm each of the following before proceeding:
  1. **Pick-up Location** – where the customer wants to be picked up.
  2. **Destination** – where the customer wants to go.
  3. **Phone Number** – the customer's contact phone number.
  4. **Date & Time** – when the customer wants the taxi (date and time).

Rules for this step:
- Collect the information naturally through conversation; you may gather multiple
  data points in a single exchange if the customer volunteers them.
- After you believe you have all 4, read them back to the customer and ask for
  explicit confirmation (e.g. "Just to confirm — you'd like a pickup at …,
  going to …, on … at …, and your phone number is …. Is that all correct?").
- If the customer corrects any detail, update it and re-confirm.
- **DO NOT** call the `make_booking` tool until the customer has verbally
  confirmed that all 4 data points are correct.

### Step 3 – Make the Booking
Once — and ONLY once — all 4 data points are confirmed, call the `make_booking`
tool with the confirmed values.

### Step 4 – Confirmation Message
After `make_booking` succeeds, tell the customer:
"Your booking has been confirmed! A confirmation has been sent to your XanhSM App.
Thank you for choosing XanhSM!"

### Step 5 – End the Call
Immediately after delivering the confirmation, call the `hang_up_call` tool to
terminate the session. Do NOT continue the conversation after this point.

## IMPORTANT CONSTRAINTS
- Never skip or reorder the steps above.
- Never fabricate information the customer hasn't provided.
- If the customer asks about anything unrelated to taxi booking, politely redirect:
  "I'm here to help you book a taxi. Shall we continue with your booking?"
- Keep responses short and natural for a phone conversation.
""".strip()


# ---------------------------------------------------------------------------
# Tool / Function Declarations (Gemini Live function-calling format)
# ---------------------------------------------------------------------------

MAKE_BOOKING_DECLARATION = {
    "name": "make_booking",
    "description": (
        "Books a taxi for the customer. Call ONLY after the customer has "
        "explicitly confirmed all 4 data points: pick-up location, destination, "
        "phone number, and date & time of booking."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "pickup_location": {
                "type": "STRING",
                "description": "Confirmed pick-up location.",
            },
            "destination": {
                "type": "STRING",
                "description": "Confirmed destination.",
            },
            "phone_number": {
                "type": "STRING",
                "description": "Customer's confirmed contact phone number.",
            },
            "date_time": {
                "type": "STRING",
                "description": "Confirmed date and time for the taxi booking.",
            },
        },
        "required": ["pickup_location", "destination", "phone_number", "date_time"],
    },
}

HANG_UP_CALL_DECLARATION = {
    "name": "hang_up_call",
    "description": (
        "Terminates the phone call after the booking confirmation has been "
        "delivered to the customer. Call this as the very last action."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {},
    },
}

TOOLS = [
    {"function_declarations": [MAKE_BOOKING_DECLARATION, HANG_UP_CALL_DECLARATION]}
]

LIVE_CONNECT_CONFIG = types.LiveConnectConfig(
    response_modalities=[types.Modality.AUDIO],
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon")
        )
    ),
    system_instruction=SYSTEM_INSTRUCTION,
    tools=TOOLS,
    # Request caller + model audio transcripts (surfaced on `server_content`).
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
)


# ---------------------------------------------------------------------------
# Mock tool implementations
# ---------------------------------------------------------------------------


def handle_make_booking(args: dict) -> dict:
    """Simulate a successful booking against the XanhSM backend."""
    logger.info("BOOKING CREATED (mock)")
    logger.info(
        "pickup=%s dest=%s phone=%s when=%s",
        args.get("pickup_location"),
        args.get("destination"),
        args.get("phone_number"),
        args.get("date_time"),
    )
    return {
        "status": "success",
        "booking_id": "XSM-20260409-0042",
        "message": "Booking confirmed. Confirmation pushed to the customer's XanhSM App.",
    }


def handle_hang_up_call(args: dict) -> dict:
    """Return payload for hang_up_call; signalling is handled in receive loop."""
    _ = args
    logger.info("hang_up_call tool invoked")
    return {"status": "success", "message": "Call terminated."}


TOOL_HANDLERS = {
    "make_booking": handle_make_booking,
    "hang_up_call": handle_hang_up_call,
}


# ---------------------------------------------------------------------------
# Gemini Live session bridged with Telcoflow
# ---------------------------------------------------------------------------


class BookingWorkflowLiveIntegration:
    """Pipes PCM between `ActiveCall` and Gemini Live, with tool-call handling."""

    def __init__(self, call: ActiveCall, gemini_session: AsyncSession) -> None:
        self._call = call
        self._gemini_session = gemini_session
        self._should_hang_up = asyncio.Event()
        self._send_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._hang_up_task: Optional[asyncio.Task] = None
        self._cleaned_up = False
        self._transcript_turns: list[tuple[str, str]] = []
        self._active_transcript_speaker: Optional[str] = None
        self._active_transcript_parts: list[str] = []
        self._transcript_flushed = False

    def _finish_active_transcript_turn(self) -> None:
        text = "".join(self._active_transcript_parts).strip()
        if text and self._active_transcript_speaker:
            self._transcript_turns.append((self._active_transcript_speaker, text))
        self._active_transcript_speaker = None
        self._active_transcript_parts = []

    def _record_transcript(self, speaker: str, transcription) -> None:
        """Store transcript fragments so the dashboard only shows final text."""
        text = transcription.text or ""
        if text:
            if self._active_transcript_speaker not in {None, speaker}:
                self._finish_active_transcript_turn()
            self._active_transcript_speaker = speaker
            self._active_transcript_parts.append(text)
            logger.info("%s transcription fragment: %s", speaker.title(), text)

        if transcription.finished:
            self._finish_active_transcript_turn()

    async def _flush_transcript(self) -> None:
        if self._transcript_flushed:
            return
        self._transcript_flushed = True
        self._finish_active_transcript_turn()
        for speaker, text in self._transcript_turns:
            await emit_transcript(self._call.call_id, speaker, text)

    async def _send_model_turn_audio(self, model_turn) -> None:
        """Forward model audio only (skip text/thought inline parts)."""
        if not model_turn:
            return
        for part in model_turn.parts:
            if part.text is not None:
                logger.info("Received text: %s", part.text)
            if not part.inline_data or not part.inline_data.data:
                continue
            data = part.inline_data.data
            if not isinstance(data, bytes):
                continue
            logger.debug(
                "send_audio: %d bytes, first4=%s",
                len(data),
                data[:4].hex() if data else "empty",
            )
            try:
                await self._call.send_audio(data)
            except BufferFullError:
                logger.warning(
                    "Send buffer full — clearing outbound audio buffer (interrupt)."
                )
                await self._call.clear_send_audio_buffer()

    async def _on_call_terminated(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        logger.info("Received call terminated event — tearing down Gemini session")
        await self._flush_transcript()
        await emit_call_ended(self._call.call_id)
        try:
            await self._gemini_session.close()
        except Exception:
            logger.exception("Error closing Gemini session")

        for task in (self._send_task, self._recv_task, self._hang_up_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except genai_errors.APIError:
                    pass

        logger.debug("Booking workflow cleanup complete")

    async def stream_to_gemini(self) -> None:
        try:
            await self._gemini_session.send_realtime_input(text=GREETING_KICKOFF_REALTIME_TEXT)
            logger.debug("Sent greeting kickoff to Gemini Live")
            async for chunk in self._call.audio_stream():
                await self._gemini_session.send_realtime_input(
                    audio=types.Blob(
                        data=chunk, mime_type=f"audio/pcm;rate={SAMPLE_RATE}"
                    )
                )
        except ConnectionClosed:
            pass
        except Exception:
            logger.exception("Error streaming caller audio to Gemini")
            raise
        finally:
            logger.debug("stream_to_gemini task completed")

    async def receive_from_gemini(self) -> None:
        try:
            while True:
                async for response in self._gemini_session.receive():
                    if content := response.server_content:
                        if content.interrupted:
                            await self._call.interrupt()
                            logger.debug("Gemini interruption — flushed outbound playback")
                            break
                        if input_transcription := content.input_transcription:
                            self._record_transcript("user", input_transcription)
                        if output_transcription := content.output_transcription:
                            self._record_transcript("agent", output_transcription)
                        await self._send_model_turn_audio(content.model_turn)

                    if response.tool_call:
                        function_responses = []
                        for fc in response.tool_call.function_calls:
                            handler = TOOL_HANDLERS.get(fc.name)
                            args = fc.args if fc.args else {}
                            await emit_tool_call(self._call.call_id, fc.name, dict(args))
                            result = handler(args) if handler else {"error": f"Unknown tool: {fc.name}"}
                            if fc.name == "make_booking":
                                await emit_booking_confirmed(
                                    self._call.call_id,
                                    dict(args),
                                    result,
                                )
                                if result.get("status") == "success":
                                    logger.info(
                                        "Booking succeeded; scheduling call hang-up"
                                    )
                                    self._should_hang_up.set()
                            function_responses.append(
                                types.FunctionResponse(
                                    id=fc.id,
                                    name=fc.name,
                                    response=result,
                                )
                            )
                            if fc.name == "hang_up_call":
                                self._should_hang_up.set()

                        await self._gemini_session.send_tool_response(
                            function_responses=function_responses
                        )

        except (ConnectionClosed, genai_errors.APIError):
            logger.debug("Gemini receive ended normally (session or connection closed)")
        except asyncio.CancelledError:
            logger.debug("receive_from_gemini task cancelled")
            raise
        except Exception:
            logger.exception("Error receiving from Gemini")
            raise
        finally:
            if not self._cleaned_up:
                with suppress(Exception):
                    await self._flush_transcript()
            logger.debug("receive_from_gemini task completed")

    async def hang_up_watcher(self) -> None:
        await self._should_hang_up.wait()
        await asyncio.sleep(POST_BOOKING_HANG_UP_DELAY_SECONDS)
        logger.info("hang_up watcher closing call")
        await self._call.close()

    async def run(self) -> None:
        self._call.register_event_handler(CallEvent.CALL_TERMINATED, self._on_call_terminated)

        self._send_task = asyncio.create_task(self.stream_to_gemini())
        self._recv_task = asyncio.create_task(self.receive_from_gemini())
        self._hang_up_task = asyncio.create_task(self.hang_up_watcher())

        await asyncio.gather(
            self._send_task,
            self._recv_task,
            self._hang_up_task,
            return_exceptions=True,
        )


async def start_gemini_session(call: ActiveCall) -> None:
    """Establish Gemini Live alongside the Telcoflow media leg for one call."""

    try:
        async with genai_client.aio.live.connect(
            model=MODEL, config=LIVE_CONNECT_CONFIG
        ) as session:
            await call.answer()
            await emit_call_answered(call.call_id)
            integration = BookingWorkflowLiveIntegration(call, session)
            await integration.run()
    except Exception:
        logger.exception("Booking workflow session failed")
        await emit_error(call.call_id, "Booking workflow session failed")
        await call.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    dashboard_task: Optional[asyncio.Task] = None
    if DASHBOARD_ENABLED:
        from dashboard_server import start_dashboard_server

        dashboard_task = asyncio.create_task(
            start_dashboard_server(DASHBOARD_HOST, DASHBOARD_PORT),
            name="dashboard-server",
        )

    _base_kw = {}
    _base_from_env = os.getenv("TELCOFLOW_BASE_URL", "").strip().strip('"').strip("'")
    if _base_from_env:
        _base_kw["base_url"] = _base_from_env

    config = TelcoflowClientConfig.sandbox(
        api_key=os.getenv("WSS_API_KEY"),
        connector_uuid=os.getenv("WSS_CONNECTOR_UUID"),
        sample_rate=SAMPLE_RATE,
        buffer_size=1024 * 1024,
        **_base_kw,
    )

    try:
        async with TelcoflowClient(config) as tf_client:

            @tf_client.on(ClientEvent.INCOMING_CALL)
            async def on_call(call: ActiveCall) -> None:
                logger.info(
                    "Incoming call id=%s — starting Gemini booking session …",
                    call.call_id,
                )
                await emit_call_started(call.call_id)
                try:
                    await start_gemini_session(call)
                except Exception:
                    logger.exception("Session failed for call_id=%s", call.call_id)
                    await emit_error(call.call_id, "Session failed")

            logger.info("XanhSM BookBot is live — waiting for calls …")
            await tf_client.run_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down …")
    finally:
        if dashboard_task:
            dashboard_task.cancel()
            with suppress(asyncio.CancelledError):
                await dashboard_task


if __name__ == "__main__":
    asyncio.run(main())
