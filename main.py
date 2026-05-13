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
"""

import asyncio
import json
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types
from telcoflow_sdk import TelcoflowClient, TelcoflowClientConfig, ActiveCall
import telcoflow_sdk.events as events

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
SAMPLE_RATE = 24000

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

# ---------------------------------------------------------------------------
# Mock tool implementations
# ---------------------------------------------------------------------------


def handle_make_booking(args: dict) -> dict:
    """Simulate a successful booking against the XanhSM backend."""
    print("\n" + "=" * 60)
    print("  BOOKING CREATED (mock)")
    print("=" * 60)
    print(f"  Pick-up  : {args.get('pickup_location')}")
    print(f"  Dest     : {args.get('destination')}")
    print(f"  Phone    : {args.get('phone_number')}")
    print(f"  Date/Time: {args.get('date_time')}")
    print("=" * 60 + "\n")
    return {
        "status": "success",
        "booking_id": "XSM-20260409-0042",
        "message": "Booking confirmed. Confirmation pushed to the customer's XanhSM App.",
    }


def handle_hang_up_call() -> dict:
    print("[hang_up_call] Ending the call.")
    return {"status": "success", "message": "Call terminated."}


TOOL_HANDLERS = {
    "make_booking": lambda args: handle_make_booking(args),
    "hang_up_call": lambda _: handle_hang_up_call(),
}

# ---------------------------------------------------------------------------
# Gemini Live session bridged with Telcoflow
# ---------------------------------------------------------------------------


async def start_gemini_session(call: ActiveCall):
    """Answer the incoming call and bridge audio with a Gemini Live session."""

    await call.answer()

    live_config = {
        "response_modalities": ["AUDIO"],
        "system_instruction": SYSTEM_INSTRUCTION,
        "tools": TOOLS,
    }

    should_hang_up = asyncio.Event()

    async with gemini_client.aio.live.connect(
        model=MODEL, config=live_config
    ) as session:

        # --- Caller → Gemini --------------------------------------------
        async def stream_to_gemini():
            async for chunk in call.audio_stream():
                await session.send_realtime_input(
                    audio=types.Blob(
                        data=chunk, mime_type=f"audio/pcm;rate={SAMPLE_RATE}"
                    )
                )

        # --- Gemini → Caller (+ tool-call handling) ----------------------
        async def receive_from_gemini():
            async for response in session.receive():
                # Audio / interruption handling
                if content := response.server_content:
                    if content.interrupted:
                        await call.clear_send_audio_buffer()
                    elif content.model_turn:
                        for part in content.model_turn.parts:
                            if part.inline_data:
                                await call.send_audio(part.inline_data.data)

                # Function-call handling
                if response.tool_call:
                    function_responses = []
                    for fc in response.tool_call.function_calls:
                        handler = TOOL_HANDLERS.get(fc.name)
                        if handler:
                            args = fc.args if fc.args else {}
                            result = handler(args)
                        else:
                            result = {"error": f"Unknown tool: {fc.name}"}

                        function_responses.append(
                            types.FunctionResponse(
                                id=fc.id,
                                name=fc.name,
                                response=result,
                            )
                        )

                        if fc.name == "hang_up_call":
                            should_hang_up.set()

                    await session.send_tool_response(
                        function_responses=function_responses
                    )

        # --- Hang-up watcher -------------------------------------------
        async def hang_up_watcher():
            """Wait for the hang_up_call signal, let final audio drain, then close."""
            await should_hang_up.wait()
            await asyncio.sleep(2)
            await call.hangup()

        await asyncio.gather(
            stream_to_gemini(),
            receive_from_gemini(),
            hang_up_watcher(),
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main():
    config = TelcoflowClientConfig.sandbox(
        api_key=os.getenv("WSS_API_KEY"),
        connector_uuid=os.getenv("WSS_CONNECTOR_UUID"),
        sample_rate=SAMPLE_RATE,
    )

    async with TelcoflowClient(config) as tf_client:

        @tf_client.on(events.INCOMING_CALL)
        async def on_call(call: ActiveCall):
            print(f"[incoming] Call received — starting Gemini session …")
            try:
                await start_gemini_session(call)
            except Exception as exc:
                print(f"[error] Session failed: {exc}")

        print("XanhSM BookBot is live — waiting for calls …")
        await tf_client.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
