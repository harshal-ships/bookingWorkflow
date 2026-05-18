const state = {
  currentCallId: null,
  activeStep: "call_started",
  eventCount: 0,
  activeTranscript: null,
  transcriptTurns: 0,
  customerTurns: 0,
  agentTurns: 0,
};

const TRANSCRIPT_MERGE_WINDOW_MS = 9000;

const el = {
  connectionBadge: document.getElementById("connectionBadge"),
  activeCall: document.getElementById("activeCall"),
  lastEvent: document.getElementById("lastEvent"),
  bookingStatus: document.getElementById("bookingStatus"),
  bookingBadge: document.getElementById("bookingBadge"),
  transcript: document.getElementById("transcript"),
  events: document.getElementById("events"),
  clearTranscript: document.getElementById("clearTranscript"),
  transcriptStatus: document.getElementById("transcriptStatus"),
  totalTurns: document.getElementById("totalTurns"),
  customerTurns: document.getElementById("customerTurns"),
  agentTurns: document.getElementById("agentTurns"),
  workflowSteps: document.querySelectorAll("#workflowSteps li"),
  pickup: document.getElementById("pickup"),
  destination: document.getElementById("destination"),
  phone: document.getElementById("phone"),
  dateTime: document.getElementById("dateTime"),
  bookingId: document.getElementById("bookingId"),
};

const eventLabels = {
  call_started: "Call connected",
  call_answered: "Call answered",
  transcript: "Transcript received",
  tool_call: "Tool call",
  booking_confirmed: "Booking confirmed",
  call_ended: "Call ended",
  error: "Error",
};

function formatTime(value) {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function setConnection(status, variant = "") {
  el.connectionBadge.textContent = status;
  el.connectionBadge.className = `badge ${variant}`.trim();
}

function resetForCall(callId) {
  state.currentCallId = callId;
  state.activeStep = "call_started";
  state.activeTranscript = null;
  state.transcriptTurns = 0;
  state.customerTurns = 0;
  state.agentTurns = 0;
  el.activeCall.textContent = callId || "Waiting";
  el.bookingStatus.textContent = "Call in progress";
  el.bookingBadge.textContent = "Collecting";
  el.bookingBadge.className = "badge badge-warning";
  el.transcriptStatus.textContent = "Call in progress";
  el.pickup.textContent = "-";
  el.destination.textContent = "-";
  el.phone.textContent = "-";
  el.dateTime.textContent = "-";
  el.bookingId.textContent = "-";
  updateTranscriptMetrics();
  renderSteps();
}

function renderSteps() {
  const order = ["call_started", "collecting", "tool_call", "booking_confirmed", "call_ended"];
  const activeIndex = order.indexOf(state.activeStep);

  el.workflowSteps.forEach((step) => {
    const index = order.indexOf(step.dataset.step);
    step.classList.toggle("active", index === activeIndex);
    step.classList.toggle("complete", index >= 0 && index < activeIndex);
  });
}

function addEvent(event) {
  state.eventCount += 1;
  el.lastEvent.textContent = eventLabels[event.type] || event.type;

  if (event.type === "transcript") {
    return;
  }

  const item = document.createElement("li");
  const title = document.createElement("strong");
  title.textContent = eventLabels[event.type] || event.type;
  const meta = document.createElement("span");
  meta.textContent = `${formatTime(event.timestamp)}${event.call_id ? ` | ${event.call_id}` : ""}`;
  item.append(title, meta);
  el.events.prepend(item);

  while (el.events.children.length > 12) {
    el.events.lastElementChild.remove();
  }
}

function shouldMergeTranscript(speaker, timestamp) {
  if (!state.activeTranscript || state.activeTranscript.speaker !== speaker) {
    return false;
  }

  const previous = new Date(state.activeTranscript.timestamp).getTime();
  const next = new Date(timestamp).getTime();
  return Number.isFinite(previous) && Number.isFinite(next) && next - previous <= TRANSCRIPT_MERGE_WINDOW_MS;
}

function joinTranscriptText(existing, incoming) {
  const current = existing.trimEnd();
  const next = incoming.trim();

  if (!current) {
    return next;
  }

  if (!next || current.endsWith(next)) {
    return current;
  }

  if (/^[.,!?;:)]/.test(next)) {
    return `${current}${next}`;
  }

  return `${current} ${next}`;
}

function updateTranscriptMetrics() {
  el.totalTurns.textContent = String(state.transcriptTurns);
  el.customerTurns.textContent = String(state.customerTurns);
  el.agentTurns.textContent = String(state.agentTurns);
}

function addTranscript(speaker, text, timestamp, isFinal = true) {
  if (!text || !text.trim()) {
    return;
  }

  const empty = el.transcript.querySelector(".empty-state");
  if (empty) {
    empty.remove();
    el.transcript.classList.remove("empty");
  }

  const normalizedSpeaker = speaker === "agent" ? "agent" : speaker === "system" ? "system" : "user";
  const shouldCountTurn = normalizedSpeaker !== "system" && isFinal;

  if (shouldMergeTranscript(normalizedSpeaker, timestamp)) {
    const body = state.activeTranscript.card.querySelector("p");
    const time = state.activeTranscript.card.querySelector("time");
    const isLiveUpdate = !isFinal || state.activeTranscript.isFinal === false;
    body.textContent = isLiveUpdate ? text.trim() : joinTranscriptText(body.textContent, text);
    time.textContent = formatTime(timestamp);
    state.activeTranscript.timestamp = timestamp;
    state.activeTranscript.isFinal = isFinal;
    el.transcript.scrollTop = el.transcript.scrollHeight;
    return;
  }

  const card = document.createElement("article");
  card.className = `message ${normalizedSpeaker}`;

  const meta = document.createElement("div");
  meta.className = "message-meta";

  const label = document.createElement("span");
  label.textContent =
    normalizedSpeaker === "agent" ? "BookBot Agent" : normalizedSpeaker === "system" ? "System" : "Customer";

  const time = document.createElement("time");
  time.textContent = formatTime(timestamp);

  const body = document.createElement("p");
  body.textContent = text.trim();

  meta.append(label, time);
  card.append(meta, body);
  el.transcript.append(card);
  if (shouldCountTurn) {
    state.transcriptTurns += 1;
    if (normalizedSpeaker === "agent") {
      state.agentTurns += 1;
    } else {
      state.customerTurns += 1;
    }
    updateTranscriptMetrics();
  }
  state.activeTranscript = {
    card,
    speaker: normalizedSpeaker,
    timestamp,
    isFinal,
  };
  el.transcript.scrollTop = el.transcript.scrollHeight;
}

function updateBooking(payload) {
  const booking = payload.booking || {};
  const result = payload.result || {};

  el.pickup.textContent = booking.pickup_location || "-";
  el.destination.textContent = booking.destination || "-";
  el.phone.textContent = booking.phone_number || "-";
  el.dateTime.textContent = booking.date_time || "-";
  el.bookingId.textContent = result.booking_id || "-";
  el.bookingStatus.textContent = result.message || "Booking confirmed";
  el.bookingBadge.textContent = "Confirmed";
  el.bookingBadge.className = "badge";
  state.activeStep = "booking_confirmed";
  renderSteps();
}

function handleEvent(event) {
  addEvent(event);

  if (event.type === "call_started") {
    resetForCall(event.call_id);
    addTranscript("system", "Incoming call connected. Transcript will be available after call completion.", event.timestamp);
    return;
  }

  if (event.type === "call_answered") {
    el.bookingStatus.textContent = "Agent connected";
    state.activeStep = "collecting";
    renderSteps();
    return;
  }

  if (event.type === "transcript") {
    if (event.payload?.is_final === false) {
      return;
    }
    const speaker = event.payload?.speaker;
    addTranscript(speaker, event.payload?.text, event.timestamp, event.payload?.is_final !== false);
    el.transcriptStatus.textContent = "Transcript extracted";
    if (state.activeStep === "call_started") {
      state.activeStep = "collecting";
      renderSteps();
    }
    return;
  }

  if (event.type === "tool_call") {
    state.activeStep = "tool_call";
    state.activeTranscript = null;
    el.bookingStatus.textContent = `Running ${event.payload?.name || "tool"}`;
    renderSteps();
    if (event.payload?.name === "make_booking") {
      const args = event.payload.args || {};
      el.pickup.textContent = args.pickup_location || "-";
      el.destination.textContent = args.destination || "-";
      el.phone.textContent = args.phone_number || "-";
      el.dateTime.textContent = args.date_time || "-";
    }
    return;
  }

  if (event.type === "booking_confirmed") {
    updateBooking(event.payload || {});
    el.transcriptStatus.textContent = "Preparing transcript";
    return;
  }

  if (event.type === "call_ended") {
    state.activeStep = "call_ended";
    state.activeTranscript = null;
    el.bookingStatus.textContent = "Call ended";
    el.transcriptStatus.textContent =
      state.transcriptTurns > 0 ? "Ready for review" : "No transcript captured";
    renderSteps();
    return;
  }

  if (event.type === "error") {
    el.bookingStatus.textContent = event.payload?.message || "Error";
    el.bookingBadge.textContent = "Needs review";
    el.bookingBadge.className = "badge badge-danger";
  }
}

function connect() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws`);

  socket.addEventListener("open", () => setConnection("Live"));
  socket.addEventListener("message", (message) => {
    handleEvent(JSON.parse(message.data));
  });
  socket.addEventListener("close", () => {
    setConnection("Reconnecting", "badge-warning");
    window.setTimeout(connect, 1500);
  });
  socket.addEventListener("error", () => setConnection("Connection error", "badge-danger"));
}

el.clearTranscript.addEventListener("click", () => {
  state.activeTranscript = null;
  state.transcriptTurns = 0;
  state.customerTurns = 0;
  state.agentTurns = 0;
  el.transcriptStatus.textContent = "Cleared";
  updateTranscriptMetrics();
  el.transcript.innerHTML = `
    <div class="empty-state">
      <strong>No transcript in this view</strong>
      <span>Final transcript data will appear here after the next completed call.</span>
    </div>
  `;
});

connect();
