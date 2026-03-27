"""
PhoneBuddy — Personal Phone Assistant
FastAPI app: Twilio webhook host + call orchestration

Workflow: 014-PPA-voice-terminal

Call pipeline follows SRCGEEE:
  S  Sensation    — Twilio fires inbound webhook; extract caller metadata
  R  Retrieve     — Load per-caller history + prior suspicion from disk
  C  Classify     — Claude Haiku classifies intent with full context
  G  Generate     — Select and render TwiML response
  E1 Execute      — Return TwiML to Twilio (route the call)
  E2 Evaluate     — Score confidence; log outcome
  E3 Evolve       — Persist call record to per-caller history; emit telemetry
"""

import os
import asyncio
import hashlib
import json
import logging
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

# Load .env from same directory as this file
load_dotenv(Path(__file__).parent / ".env")
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
import httpx

# ── Logging ──────────────────────────────────────────────────────────────────
_log_dir = Path("data/logs")
_log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_dir / "phonebuddy.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("phonebuddy")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="PhoneBuddy", version="0.1.0")

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get("PHONEBUDDY_CONFIG", "config/user-profile.yaml")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

# ── TTS — ElevenLabs receptionist / Polly IVR ────────────────────────────────

_tts_cache: dict[str, bytes] = {}  # keyed by "role:text" — avoids re-generating identical phrases


async def _tts_elevenlabs(text: str, cfg: dict, voice_role: str = "receptionist") -> bytes:
    """Generate MP3 audio via ElevenLabs. Results are cached in-process."""
    cache_key = f"{voice_role}:{text}"
    if cache_key in _tts_cache:
        return _tts_cache[cache_key]

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise ValueError("ELEVENLABS_API_KEY not set")

    voice_cfg = cfg.get("voice", {}).get(voice_role, {})
    voice_id  = voice_cfg.get("voice_id", "TX3LPaxmHKxFdv7VOQHJ")   # Liam
    model_id  = voice_cfg.get("model",    "eleven_turbo_v2_5")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": model_id,
                "voice_settings": {
                    "stability":        voice_cfg.get("stability",        0.5),
                    "similarity_boost": voice_cfg.get("similarity_boost", 0.75),
                },
            },
        )
        resp.raise_for_status()

    _tts_cache[cache_key] = resp.content
    log.info(f"TTS generated  role={voice_role}  chars={len(text)}  bytes={len(resp.content)}")
    return resp.content


def _play(text: str, base_url: str, role: str = "receptionist") -> str:
    """Return a TwiML <Play> element that fetches ElevenLabs audio from our /tts endpoint.
    Note: '&' must be escaped as '&amp;' inside XML text content (TwiML is XML).
    """
    encoded = urllib.parse.quote(text, safe="")
    return f'<Play>{base_url}/tts?text={encoded}&amp;role={role}</Play>'


@app.get("/tts")
async def tts_serve(text: str, role: str = "receptionist"):
    """
    On-demand TTS endpoint — Twilio fetches this URL via <Play>.
    Streams ElevenLabs MP3 audio back to Twilio.
    Falls back gracefully on error (Twilio skips a failed Play).
    """
    cfg = load_config()
    try:
        audio = await _tts_elevenlabs(text, cfg, role)
        return Response(content=audio, media_type="audio/mpeg")
    except Exception as e:
        log.error(f"TTS serve error: {e}")
        return Response(status_code=500)


# ── Per-caller history (R step — persisted to data/history/<number>.jsonl) ───

HISTORY_DIR = Path("data/history")
HISTORY_MAX_RECORDS = 10  # per caller, most recent


def _caller_history_path(caller_number: str) -> Path:
    safe = caller_number.lstrip("+").replace("-", "").replace(" ", "")
    return HISTORY_DIR / f"{safe}.jsonl"


def _load_history(caller_number: str) -> list[dict]:
    """Load the N most recent call records for this caller."""
    path = _caller_history_path(caller_number)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records[-HISTORY_MAX_RECORDS:]


def _save_history_record(caller_number: str, record: dict) -> None:
    """Append one call outcome to this caller's history file."""
    path = _caller_history_path(caller_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


async def _retrieve_context(caller_number: str, cfg: dict) -> dict:
    """
    R step — retrieve everything known about this caller before classification.
    Returns a context dict consumed by _classify_with_claude and routing logic.
    """
    contact = _match_contact(caller_number, "", cfg)
    history = _load_history(caller_number)

    # Compute prior suspicion from call history
    prior_suspicion = 0.0
    if history:
        bad_calls = [h for h in history if h.get("classification") in ("scam", "solicitation")]
        if bad_calls:
            # Each bad call adds 0.20, capped at 0.60 so Claude can still override
            prior_suspicion = min(0.60, len(bad_calls) * 0.20)

    return {
        "contact": contact,
        "history": history,
        "call_count": len(history),
        "prior_suspicion": prior_suspicion,
        "repeat_bad_actor": prior_suspicion >= 0.60,
    }


def _evolve_context(
    call_sid: str,
    caller_number: str,
    classification: str,
    outcome: str,
    cfg: dict,
) -> None:
    """
    E3 step — persist this call's outcome to per-caller history and emit telemetry.
    Call this just before returning each TwiML response so outcome is recorded.
    """
    call = active_calls.get(call_sid, {})
    record = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "classification": classification,
        "outcome": outcome,
        "suspicion_score": round(call.get("suspicion_score", 0.0), 3),
        "transcript_words": sum(
            len(t.split()) for t in call.get("transcript", [])
        ),
    }
    _save_history_record(caller_number, record)
    _emit_telemetry(call_sid, classification, outcome, cfg)
    log.info(
        f"E3/Evolve  SID={call_sid}  caller={caller_number}"
        f"  classification={classification}  outcome={outcome}"
    )


# ── Active calls (in-memory for MVP) ─────────────────────────────────────────
active_calls: dict[str, dict] = {}

# ── WebSocket dashboard clients ───────────────────────────────────────────────
dashboard_clients: list[WebSocket] = []

async def broadcast_dashboard(event: dict):
    """Push call event to all connected dashboard browsers."""
    payload = json.dumps(event)
    for ws in list(dashboard_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dashboard_clients.remove(ws)


# ═══════════════════════════════════════════════════════════════════════════════
# TWILIO WEBHOOKS
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/call/inbound")
async def inbound_call(
    request: Request,
    CallSid: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
    CallStatus: str = Form(default="ringing"),
):
    """
    Step 1 — Twilio fires this when a call arrives.
    We answer silently (the Hello Trap) and open a Media Stream for STT.
    """
    cfg = load_config()
    log.info(f"Inbound call: {From} → {To}  SID={CallSid}")

    active_calls[CallSid] = {
        "sid": CallSid,
        "from": From,
        "to": To,
        "started": datetime.utcnow().isoformat(),
        "status": "answering",
        "transcript": [],
        "classification": None,
        "suspicion_score": 0.0,
    }

    await broadcast_dashboard({
        "event": "call_start",
        "sid": CallSid,
        "from": From,
        "status": "answering — silence trap active",
    })

    # ── SILENCE TRAP ──
    # Answer with dead silence. Autodialers wait for voice activity.
    # Real humans say "Hello?" — we catch that in /call/classify.
    # A <Gather> with a long pause + speech detection handles both.
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play("Hello, I am Nicholas's personal assistant. This call may be recorded.", base_url)}
  <Gather input="speech" action="{base_url}/call/classify"
          speechTimeout="auto" timeout="4" language="en-US">
    <Pause length="3"/>
  </Gather>
  {_play("Hello, are you there? I don't recognize your number. Would you like to leave a message?", base_url)}
  <Gather input="speech" action="{base_url}/call/classify"
          speechTimeout="auto" timeout="4" language="en-US">
    <Pause length="3"/>
  </Gather>
  <Redirect>{base_url}/call/voicemail</Redirect>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


@app.post("/call/classify")
async def classify_call(
    request: Request,
    CallSid: str = Form(...),
    From: str = Form(...),
    SpeechResult: str = Form(default=""),
    Confidence: float = Form(default=0.0),
):
    """
    SRCGEEE pipeline — Twilio posts here with the caller's speech.
    Each labelled block is one phase of the pipeline.
    """
    cfg = load_config()

    # ── S: SENSATION ──────────────────────────────────────────────────────────
    # Normalize the inbound signal: transcript + caller metadata.
    transcript_text = SpeechResult.strip()
    caller_number = From
    log.info(f"S/Sensation  SID={CallSid}  from={caller_number}  speech='{transcript_text}'  conf={Confidence:.2f}")

    if CallSid in active_calls:
        active_calls[CallSid]["transcript"].append(transcript_text)

    # ── R: RETRIEVE ───────────────────────────────────────────────────────────
    # Pull everything known about this caller before reasoning begins.
    context = await _retrieve_context(caller_number, cfg)
    suspicion = context["prior_suspicion"]  # carry forward from history
    log.info(
        f"R/Retrieve  SID={CallSid}  history_calls={context['call_count']}"
        f"  prior_suspicion={suspicion:.2f}  repeat_bad={context['repeat_bad_actor']}"
    )

    # Fast-path: known repeat bad actor — skip Claude, block immediately.
    if context["repeat_bad_actor"]:
        log.info(f"R/FastBlock  SID={CallSid}  reason=repeat_bad_actor")
        _evolve_context(CallSid, caller_number, "scam", "fast_blocked", cfg)
        return await _decline_solicitation(request, CallSid, cfg)

    # Safe word check — owner calling in (no history lookup needed)
    safe_word = cfg["user"]["safe_word"].lower()
    safe_word_alt = cfg["user"].get("safe_word_alt", "").lower()
    if safe_word in transcript_text.lower() or safe_word_alt in transcript_text.lower():
        if caller_number == cfg["user"]["cell"]:
            log.info(f"Admin mode activated  SID={CallSid}")
            return await _admin_mode_response(request, CallSid, transcript_text, cfg)

    # Known contact from whitelist — forward immediately (no Claude needed)
    contact = context["contact"] or _match_contact(caller_number, transcript_text, cfg)
    if contact:
        if "self" in contact.get("tags", []):
            return await _admin_mode_response(request, CallSid, transcript_text, cfg)
        _evolve_context(CallSid, caller_number, "contact", "forwarded", cfg)
        return await _forward_to_cell(request, CallSid, contact, cfg)

    # ── C: CLASSIFY ───────────────────────────────────────────────────────────
    # Claude Haiku classifies intent with full context (history + transcript).
    suspicion += _score_name_formality(transcript_text, cfg)
    classification, confidence, suspicion_delta = await _classify_with_claude(
        transcript_text, caller_number, cfg, context
    )
    suspicion += suspicion_delta
    suspicion = min(1.0, max(0.0, suspicion))
    log.info(
        f"C/Classify  SID={CallSid}  intent={classification}"
        f"  confidence={confidence:.2f}  suspicion={suspicion:.2f}"
    )

    if CallSid in active_calls:
        active_calls[CallSid]["classification"] = classification
        active_calls[CallSid]["suspicion_score"] = suspicion

    # ── G: GENERATE ───────────────────────────────────────────────────────────
    # Select routing strategy based on classification + suspicion score.
    await broadcast_dashboard({
        "event": "classification",
        "sid": CallSid,
        "from": From,
        "transcript": transcript_text,
        "classification": classification,
        "suspicion_score": round(suspicion, 2),
        "confidence": round(confidence, 2),
        "history_calls": context["call_count"],
    })

    thresholds = cfg["thresholds"]

    # ── E1: EXECUTE + E3: EVOLVE ──────────────────────────────────────────────
    # Each branch executes (returns TwiML) then evolves (saves history).

    if classification in ("medical", "professional"):
        _evolve_context(CallSid, caller_number, classification, "escalated_hitl", cfg)
        return await _hold_and_brief(request, CallSid, transcript_text, classification, cfg)

    if suspicion >= thresholds["scam_engage"]:
        _evolve_context(CallSid, caller_number, "scam", "engaging", cfg)
        return await _engage_scam(request, CallSid, cfg)

    if suspicion >= thresholds["solicitation_block"]:
        _evolve_context(CallSid, caller_number, "solicitation", "declined", cfg)
        return await _decline_solicitation(request, CallSid, cfg)

    # Unknown / low suspicion — ask for more context (3-strikes loop)
    attempt = len(active_calls.get(CallSid, {}).get("transcript", []))
    if attempt >= thresholds["max_classification_attempts"]:
        _evolve_context(CallSid, caller_number, classification, "voicemail", cfg)
        return await _take_voicemail(request, CallSid, cfg)

    return await _ask_purpose(request, CallSid, cfg)


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTING RESPONSES
# ═══════════════════════════════════════════════════════════════════════════════

async def _forward_to_cell(request: Request, call_sid: str, contact: dict, cfg: dict) -> Response:
    """Known contact — forward immediately with optional whisper briefing."""
    cell = cfg["user"]["cell"]
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    name = contact.get("name", "someone from your contacts")
    log.info(f"Forwarding to cell for contact '{name}'  SID={call_sid}")

    await broadcast_dashboard({"event": "forwarding", "sid": call_sid, "contact": name})

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play("Please hold one moment.", base_url)}
  <Dial callerId="{cfg['user'].get('landline') or cfg['user']['cell']}">
    <Number url="{base_url}/call/whisper?name={name}">{cell}</Number>
  </Dial>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


async def _hold_and_brief(request: Request, call_sid: str, transcript: str,
                          classification: str, cfg: dict) -> Response:
    """Medical/professional — hold caller, call user cell with briefing."""
    cell = cfg["user"]["cell"]
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    log.info(f"Medical/professional hold  SID={call_sid}")

    await broadcast_dashboard({
        "event": "hitl_escalation",
        "sid": call_sid,
        "classification": classification,
        "transcript": transcript,
    })

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play("Thank you. Please hold one moment while I connect you.", base_url)}
  <Enqueue waitUrl="{base_url}/call/hold-music">{call_sid}_queue</Enqueue>
</Response>"""
    # TODO Phase 2: trigger outbound call to cell with whisper briefing
    return Response(content=twiml, media_type="application/xml")


async def _decline_solicitation(request: Request, call_sid: str, cfg: dict) -> Response:
    """Solicitation — polite robotic decline."""
    log.info(f"Declining solicitation  SID={call_sid}")
    await broadcast_dashboard({"event": "declined", "sid": call_sid, "reason": "solicitation"})
    _emit_telemetry(call_sid, "solicitation", "declined", cfg)

    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joey">
    Thank you for calling. We are not interested at this time.
    Please remove this number from your list. Goodbye.
  </Say>
  <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


async def _engage_scam(request: Request, call_sid: str, cfg: dict) -> Response:
    """Scam — slow deliberate engagement to waste caller's time."""
    log.info(f"Engaging scam caller  SID={call_sid}")
    await broadcast_dashboard({"event": "engaging_scam", "sid": call_sid})

    # Rotate through engagement responses to seem like a confused older adult
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joey">
    Oh, yes, hello. Could you repeat that? I want to make sure I understand.
    My hearing is not what it used to be.
  </Say>
  <Gather input="speech" action="/call/classify" speechTimeout="4" timeout="8">
    <Pause length="1"/>
  </Gather>
  <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


async def _ask_purpose(request: Request, call_sid: str, cfg: dict) -> Response:
    """Unknown caller — ask them to identify themselves."""
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play("Hello, thank you for calling. May I ask who is calling and the nature of your call?", base_url)}
  <Gather input="speech" action="{base_url}/call/classify"
          speechTimeout="auto" timeout="10">
  </Gather>
  <Redirect>{base_url}/call/voicemail</Redirect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


async def _take_voicemail(request: Request, call_sid: str, cfg: dict) -> Response:
    """After max classification attempts — take a message."""
    log.info(f"Taking voicemail  SID={call_sid}")
    await broadcast_dashboard({"event": "voicemail", "sid": call_sid})
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play("I'm sorry, the person you are trying to reach is unavailable. Please leave a message after the tone.", base_url)}
  <Record maxLength="60" transcribe="true"
          action="{base_url}/call/recording-complete"
          transcribeCallback="{base_url}/call/recording-complete"/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/call/admin-query")
async def admin_query(
    request: Request,
    CallSid: str = Form(default=""),
    SpeechResult: str = Form(default=""),
):
    """Owner spoke a command in admin mode. Echo back a placeholder for now."""
    cfg = load_config()
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    query = SpeechResult.strip() or "I didn't catch that."
    log.info(f"Admin query  SID={CallSid}  query='{query}'")

    # TODO Phase 2: parse command intent (recent calls, hold proxy, whitelist add, etc.)
    reply = f"You said: {query}. Admin commands are coming in the next update. Goodbye for now."
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(reply, base_url)}
  <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/call/voicemail")
async def voicemail_route(request: Request, CallSid: str = Form(default="")):
    """Twilio redirects here after exhausting classification attempts."""
    cfg = load_config()
    return await _take_voicemail(request, CallSid, cfg)


@app.post("/call/recording-complete")
async def recording_complete(
    request: Request,
    CallSid: str = Form(default=""),
    RecordingUrl: str = Form(default=""),
    RecordingDuration: str = Form(default="0"),
    TranscriptionText: str = Form(default=""),
    TranscriptionStatus: str = Form(default=""),
    From: str = Form(default=""),
):
    """
    Twilio posts here when a recording (and optional transcription) is ready.
    transcribe='true' on <Record> makes Twilio POST TranscriptionText here.
    Logs a JSON record to data/calls/<CallSid>.json.
    """
    log.info(
        f"Recording complete  SID={CallSid}  duration={RecordingDuration}s"
        f"  transcription_status={TranscriptionStatus}"
    )

    # Resolve caller number: prefer active_calls, fall back to Form field
    call_info = active_calls.get(CallSid, {})
    caller_number = call_info.get("from") or From or "unknown"

    record = {
        "call_sid": CallSid,
        "caller": caller_number,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "duration_sec": int(RecordingDuration) if RecordingDuration.isdigit() else 0,
        "recording_url": RecordingUrl,
        "transcript": TranscriptionText,
        "transcription_status": TranscriptionStatus,
        "classification": call_info.get("classification", "unknown"),
        "suspicion_score": call_info.get("suspicion_score", 0.0),
    }

    calls_dir = Path("data/calls")
    calls_dir.mkdir(parents=True, exist_ok=True)
    record_path = calls_dir / f"{CallSid}.json"
    record_path.write_text(json.dumps(record, indent=2))
    log.info(f"Call record saved: {record_path}")

    await broadcast_dashboard({
        "event": "recording_saved",
        "sid": CallSid,
        "caller": caller_number,
        "duration_sec": record["duration_sec"],
        "transcript": TranscriptionText,
    })

    return Response(content="<?xml version='1.0'?><Response/>", media_type="application/xml")


async def _admin_mode_response(request: Request, call_sid: str,
                                transcript: str, cfg: dict) -> Response:
    """Owner safe word detected — respond as admin assistant."""
    log.info(f"Admin mode  SID={call_sid}  query='{transcript}'")
    await broadcast_dashboard({"event": "admin_mode", "sid": call_sid})

    # Get call summary from active calls log
    total_today = len(active_calls)
    summary = f"Hello Nick. You have had {total_today} calls today. How can I help you?"

    # TODO Phase 2: query call log, trigger outbound hold proxy, etc.
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(summary, base_url)}
  <Gather input="speech" action="{base_url}/call/admin-query" speechTimeout="5" timeout="10">
    <Pause length="1"/>
  </Gather>
  <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# ═══════════════════════════════════════════════════════════════════════════════
# WHISPER BRIEFING
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/call/whisper")
async def whisper_briefing(request: Request, name: str = "someone"):
    """Plays briefing to Nick before the call is connected."""
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(f"PhoneBuddy: {name} is on the line. Press any key to connect, or hang up to decline.", PUBLIC_URL or str(request.base_url).rstrip('/'))}
  <Gather numDigits="1" action="/call/whisper-response">
    <Pause length="5"/>
  </Gather>
  <!-- Auto-connect if no key pressed -->
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# ═══════════════════════════════════════════════════════════════════════════════
# CLASSIFICATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _score_name_formality(transcript: str, cfg: dict) -> float:
    """Returns suspicion delta based on how caller addressed the user."""
    t = transcript.lower()
    user = cfg["user"]
    signals = cfg.get("name_signals", {})

    if user.get("alternate_handle", "").lower() in t:
        return signals.get("alternate_handle", -0.30)
    if user.get("preferred_name", "").lower() in t:
        return signals.get("preferred_name", -0.20)
    if f"{user.get('formal_name','').lower()} {user.get('last_name','').lower()}" in t:
        return signals.get("full_formal", +0.50)
    if user.get("formal_name", "").lower() in t:
        return signals.get("formal_name", +0.35)
    if user.get("last_name", "").lower() in t:
        return signals.get("last_name_only", +0.40)
    return signals.get("no_name", 0.0)


def _match_contact(caller_number: str, transcript: str, cfg: dict) -> Optional[dict]:
    """Returns matching contact dict or None."""
    t = transcript.lower()
    for contact in cfg.get("contacts", []):
        if caller_number in contact.get("numbers", []):
            return contact
        for kw in contact.get("keyword_match", []):
            if kw.lower() in t:
                return contact
    return None


async def _classify_with_claude(
    transcript: str,
    caller_number: str,
    cfg: dict,
    context: Optional[dict] = None,
) -> tuple[str, float, float]:
    """
    C step — Claude Haiku classifies caller intent.
    Context from the R step (history, prior suspicion) is injected into the prompt.
    Returns: (classification, confidence, suspicion_delta)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set — defaulting to unknown")
        return "unknown", 0.5, 0.0

    # Build history context string for the prompt
    history_context = ""
    if context and context.get("history"):
        recent = context["history"][-3:]
        classifications = [h.get("classification", "unknown") for h in recent]
        outcomes = [h.get("outcome", "?") for h in recent]
        history_context = (
            f"\nCall history for this number (oldest→newest): "
            f"{list(zip(classifications, outcomes))}"
        )

    prompt = f"""You are classifying an inbound phone call to determine how to route it.

Caller number: {caller_number}
Caller said: "{transcript}"{history_context}

Classify into exactly one of:
- contact: personal or business contact the owner knows
- medical: doctor, hospital, pharmacy, insurance (health-related callback)
- professional: attorney, accountant, government agency, legitimate business
- solicitation: charity, sales pitch, political, survey, marketing
- scam: fraud attempt, fake prize, IRS impersonation, tech support scam
- unknown: cannot determine from available information

Rules:
- If call history shows prior scam/solicitation classifications, weight suspicion_delta higher.
- If transcript is empty or garbled, return unknown with low confidence.
- suspicion_delta range: -0.5 (clearly legitimate) to +0.5 (clearly malicious).

Respond with JSON only, no markdown:
{{"classification": "...", "confidence": 0.0-1.0, "suspicion_delta": -0.5 to +0.5, "reasoning": "one sentence"}}"""

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": cfg.get("cost", {}).get("model_routing", {}).get(
                        "standard", "claude-haiku-4-5-20251001"
                    ),
                    "max_tokens": 150,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            data = resp.json()
            text = data["content"][0]["text"].strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            result = json.loads(text)
            log.info(f"C/Claude  classification={result.get('classification')}  reasoning={result.get('reasoning', '')}")
            return (
                result.get("classification", "unknown"),
                float(result.get("confidence", 0.5)),
                float(result.get("suspicion_delta", 0.0)),
            )
    except Exception as e:
        log.error(f"Claude classification error: {e}")
        return "unknown", 0.5, 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# TELEMETRY
# ═══════════════════════════════════════════════════════════════════════════════

def _emit_telemetry(call_sid: str, classification: str, outcome: str, cfg: dict):
    """Write privacy-screened telemetry record to local JSONL."""
    if not cfg.get("telemetry", {}).get("enabled", True):
        return

    call = active_calls.get(call_sid, {})
    transcript = " ".join(call.get("transcript", []))

    record = {
        "schema_version": "1.0",
        "pattern_hash": "sha256:" + hashlib.sha256(
            f"{classification}:{len(transcript.split())}".encode()
        ).hexdigest(),
        "classification": classification,
        "confidence": call.get("suspicion_score", 0.0),
        "outcome": outcome,
        "duration_sec": 0,  # TODO: calculate from started timestamp
        "timestamp_hour": datetime.utcnow().hour,
        "language_detected": "en",
    }

    log_path = cfg.get("telemetry", {}).get("local_log", "data/telemetry/calls.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket):
    """Browser connects here to receive live call events."""
    await websocket.accept()
    dashboard_clients.append(websocket)
    log.info("Dashboard client connected")
    try:
        # Send current active calls on connect
        await websocket.send_text(json.dumps({
            "event": "init",
            "active_calls": list(active_calls.values()),
        }))
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        dashboard_clients.remove(websocket)
        log.info("Dashboard client disconnected")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Live call activity dashboard — opens in browser at localhost:8000/dashboard"""
    return HTMLResponse(DASHBOARD_HTML)


DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<title>PhoneBuddy Dashboard</title>
<style>
  body { font-family: monospace; background: #0d1117; color: #c9d1d9; padding: 20px; }
  h1 { color: #58a6ff; }
  .call { border: 1px solid #30363d; padding: 12px; margin: 8px 0; border-radius: 6px; }
  .call.scam { border-color: #f85149; }
  .call.medical { border-color: #3fb950; }
  .call.forwarding { border-color: #58a6ff; }
  .call.admin { border-color: #d2a8ff; }
  .label { font-size: 11px; color: #8b949e; text-transform: uppercase; }
  .score { font-size: 22px; font-weight: bold; }
  .transcript { color: #e6edf3; margin-top: 8px; font-style: italic; }
  #status { color: #3fb950; }
  h2 { color: #8b949e; font-size: 14px; text-transform: uppercase; letter-spacing: 1px; margin-top: 32px; }
  .rec-call { border: 1px solid #21262d; padding: 10px 14px; margin: 6px 0; border-radius: 6px; font-size: 13px; }
  .rec-call .meta { color: #8b949e; font-size: 11px; margin-bottom: 4px; }
  .rec-call .tx { color: #e6edf3; font-style: italic; margin-top: 4px; }
</style>
</head>
<body>
<h1>📞 PhoneBuddy</h1>
<p id="status">Connecting...</p>
<div id="feed"></div>
<h2>Recent Calls</h2>
<div id="recent-calls"><em style="color:#8b949e">Loading...</em></div>
<script>
// ── Recent calls panel ──────────────────────────────────────────────────────
function renderRecentCalls(calls) {
  const el = document.getElementById('recent-calls');
  if (!calls || calls.length === 0) {
    el.innerHTML = '<em style="color:#8b949e">No recorded calls yet.</em>';
    return;
  }
  el.innerHTML = calls.map(c => {
    const ts = c.timestamp ? new Date(c.timestamp).toLocaleString() : '';
    const dur = c.duration_sec ? `${c.duration_sec}s` : '';
    const tx = c.transcript ? `<div class="tx">"${c.transcript}"</div>` : '';
    const cls = c.classification && c.classification !== 'unknown' ? ` &mdash; <b>${c.classification}</b>` : '';
    return `<div class="rec-call">
      <div class="meta">${ts}${dur ? ' &bull; ' + dur : ''}${cls}</div>
      <div>${c.caller || 'unknown'}</div>${tx}
    </div>`;
  }).join('');
}

async function loadRecentCalls() {
  try {
    const r = await fetch('/dashboard/calls');
    const data = await r.json();
    renderRecentCalls(data.calls);
  } catch(e) {
    document.getElementById('recent-calls').innerHTML = '<em style="color:#f85149">Failed to load.</em>';
  }
}

loadRecentCalls();

const ws = new WebSocket(`ws://${location.host}/ws/dashboard`);
ws.onopen = () => document.getElementById('status').textContent = 'Live ✓';
ws.onclose = () => document.getElementById('status').textContent = 'Disconnected';
ws.onmessage = (msg) => {
  const e = JSON.parse(msg.data);
  // Refresh recent calls panel whenever a recording is saved
  if (e.event === 'recording_saved') loadRecentCalls();
  const feed = document.getElementById('feed');
  const div = document.createElement('div');

  const cls = e.classification || e.event || '';
  div.className = 'call ' + (
    cls.includes('scam') ? 'scam' :
    cls.includes('medical') ? 'medical' :
    e.event === 'forwarding' ? 'forwarding' :
    e.event === 'admin_mode' ? 'admin' : ''
  );

  const score = e.suspicion_score !== undefined
    ? `<span class="score" style="color:${e.suspicion_score > 0.7 ? '#f85149' : '#3fb950'}">${(e.suspicion_score*100).toFixed(0)}%</span>`
    : '';

  div.innerHTML = `
    <div class="label">${new Date().toLocaleTimeString()} — ${e.event}</div>
    <div><b>${e.from || ''}</b> ${score}</div>
    ${e.classification ? `<div>Classification: <b>${e.classification}</b> (${(e.confidence*100||0).toFixed(0)}% conf)</div>` : ''}
    ${e.transcript ? `<div class="transcript">"${e.transcript}"</div>` : ''}
    ${e.contact ? `<div>Contact: ${e.contact}</div>` : ''}
    ${e.reason ? `<div>Reason: ${e.reason}</div>` : ''}
  `;
  feed.prepend(div);
};
</script>
</body>
</html>"""


@app.get("/dashboard/calls")
async def dashboard_calls():
    """Return the last 10 call records from data/calls/ as JSON."""
    calls_dir = Path("data/calls")
    calls_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for path in sorted(calls_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
        try:
            records.append(json.loads(path.read_text()))
        except Exception as exc:
            log.warning(f"Skipping bad call record {path}: {exc}")
    return {"calls": records}


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    return {"status": "ok", "product": "PhoneBuddy", "version": "0.1.0"}


@app.get("/debug-twiml")
def debug_twiml():
    """Return the TwiML that would be sent to Twilio for an inbound call."""
    base_url = PUBLIC_URL or "NOT_SET"
    twiml_url = f"{base_url}/tts?text=Hello&role=receptionist"
    return {
        "PUBLIC_URL": PUBLIC_URL,
        "base_url": base_url,
        "play_url": twiml_url,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
