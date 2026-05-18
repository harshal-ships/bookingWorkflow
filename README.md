# XanhSM Passenger Booking Workflow - Voice AI Agent

A voice-based taxi booking agent powered by the **Gemini Live API** (native audio) and **Telcoflow SDK** (telephony). When a customer calls in, the agent follows a strict state-machine workflow to collect booking details and confirm a taxi reservation.

## Workflow

```
Greeting → Collect 4 data points → Confirm → make_booking → Notify user → hang_up_call
```

| # | Step | Description |
|---|------|-------------|
| 1 | **Greeting & Intent** | Greet the caller; identify taxi-booking intent |
| 2 | **Collect & Confirm** | Gather: pick-up location, destination, phone number, date/time — then read back and get explicit confirmation |
| 3 | **Book** | Call `make_booking` tool with all 4 confirmed fields |
| 4 | **Confirm** | Tell the customer a confirmation was sent to their XanhSM App |
| 5 | **Hang Up** | Call `hang_up_call` tool to terminate the session |

## Prerequisites

- Python 3.11+
- A **Google API key** with Gemini Live API access
- **Telcoflow** credentials (API key + connector UUID) from the [Telcoflow dashboard](https://portal.telcoflow.io)

## Setup

```bash
# Clone / navigate to the project
cd bookingWorkflow

# Create a virtual environment
python -m venv .venv && source .venv/bin/activate

# Install dependencies (telcoflow-sdk is on Test PyPI)
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Edit .env with your credentials
```

## Run

```bash
python main.py
```

The agent will start listening for incoming calls via Telcoflow. Dial the number associated with your connector UUID to begin a booking session.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google API key with Gemini Live access |
| `WSS_API_KEY` | Telcoflow API key |
| `WSS_CONNECTOR_UUID` | Telcoflow connector UUID |

## Project Structure

```
bookingWorkflow/
├── main.py            # Agent entrypoint — all workflow logic
├── requirements.txt   # Python dependencies
├── .env.example       # Template for environment variables
└── README.md          # This file
```
