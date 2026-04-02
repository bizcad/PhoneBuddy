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
import sqlite3
import urllib.parse
from contextlib import asynccontextmanager
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
from fastapi.templating import Jinja2Templates
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

# ── SQLite — schema init ──────────────────────────────────────────────────────
_PB_DATA_DIR = Path(os.environ.get("PB_DATA_DIR", "/data"))
_DB_PATH = _PB_DATA_DIR / "phonebuddy.db"

_CREATE_CONTACTS = """
CREATE TABLE IF NOT EXISTS contacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phone      TEXT    UNIQUE NOT NULL,
    name       TEXT    NOT NULL,
    notes      TEXT,
    tags       TEXT,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);
"""

_CREATE_CALL_HISTORY = """
CREATE TABLE IF NOT EXISTS call_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid         TEXT    UNIQUE NOT NULL,
    phone            TEXT    NOT NULL,
    name             TEXT,
    classification   TEXT,
    suspicion_score  REAL,
    outcome          TEXT,
    transcript       TEXT,
    started_at       TEXT    NOT NULL,
    ended_at         TEXT
);
"""


def get_db() -> sqlite3.Connection:
    """Open (or create) the SQLite DB and return a connection with WAL mode enabled."""
    _PB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist yet. Called at app startup."""
    conn = get_db()
    try:
        conn.execute(_CREATE_CONTACTS)
        conn.execute(_CREATE_CALL_HISTORY)
        conn.commit()
        log.info("SQLite schema ready: %s", _DB_PATH)
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler — runs init_db before accepting requests."""
    init_db()
    yield


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="PhoneBuddy", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get("PHONEBUDDY_CONFIG", "config/user-profile.yaml")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
PPA_URL = os.environ.get("PPA_URL", "").strip().rstrip("/")


async def _post_ppa_sensation(caller_id: str, classification: str, outcome: str, transcript: list[str]) -> None:
    """Fire-and-forget: send call summary to PPA /sensation."""
    if not PPA_URL:
        return
    payload = {
        "input": f"Call ended. Classification: {classification}. Outcome: {outcome}. Transcript: {' | '.join(transcript[-5:])}",
        "source": "phonebuddy",
        "caller_id": caller_id,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{PPA_URL}/sensation", json=payload)
    except Exception as exc:
        log.warning("PPA sensation post failed: %s", exc)


def load_config() -> dict:
    """Load config from YAML, then overlay any env vars set by Ted at deploy time.
    YAML = factory defaults. Env vars = per-customer configuration.
    All overrides are optional — missing env vars leave the YAML value intact.
    """
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    # ── Identity ──────────────────────────────────────────────────────────────
    if v := os.environ.get("OWNER_NAME"):
        cfg["user"]["preferred_name"] = v
    if v := os.environ.get("OWNER_CELL"):
        cfg["user"]["cell"] = v
    if v := os.environ.get("SAFE_WORD"):
        cfg["user"]["safe_word"] = v
    if v := os.environ.get("SAFE_WORD_ALT"):
        cfg["user"]["safe_word_alt"] = v

    # ── Persona (from persona builder) ───────────────────────────────────────
    # PB_VOICE_TONE     : warm | professional | efficient | protective | playful
    # PB_TRANSPARENCY   : disclosed | answering_service | honest_if_asked
    # PB_RECORDING_MODE : full | summary_only | log_only | none
    # PB_ESCALATION     : text_summary | direct_interrupt | schedule_callback | fully_autonomous
    if v := os.environ.get("PB_VOICE_TONE"):
        cfg.setdefault("persona", {}).setdefault("voice", {})["tone"] = v
    if v := os.environ.get("PB_TRANSPARENCY"):
        cfg.setdefault("persona", {})["transparency"] = v
    if v := os.environ.get("PB_RECORDING_MODE"):
        cfg.setdefault("persona", {}).setdefault("recording", {})["mode"] = v
    if v := os.environ.get("PB_ESCALATION"):
        cfg.setdefault("persona", {}).setdefault("escalation", {})["mode"] = v

    # ── Voice ─────────────────────────────────────────────────────────────────
    if v := os.environ.get("ELEVENLABS_VOICE_ID"):
        cfg["voice"]["receptionist"]["voice_id"] = v

    return cfg

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


def _play_filler(filename: str, base_url: str) -> str:
    """Return a TwiML <Play> element for a pre-recorded filler WAV.
    Faster than TTS — no ElevenLabs round-trip. Sounds like Nick.
    Files live in static/audio/fillers/.
    """
    return f'<Play>{base_url}/static/audio/fillers/{filename}</Play>'


# ── Filler Chains ─────────────────────────────────────────────────────────────
# Each chain is 2-3 short WAVs that play sequentially before the <Redirect>.
# The combined duration buys time for Haiku + ElevenLabs to complete.
# Add new chains as Nick records new fillers.
FILLER_CHAINS: dict[str, list[str]] = {
    # Default: caller said something thoughtful, PB is processing
    "thinking":   ["hmmm(pondering).wav", "i-want-to-get-this-right.wav"],
    # Caller said something surprising or unexpected
    "surprised":  ["oh(slight-surprise).wav", "hmmm(pondering).wav"],
    # Caller is engaged, agreeing, or enthusiastic
    "engaged":    ["oh-yeah(affirmative).wav", "let-me-think-about-that.wav"],
    # Caller is skeptical, pushing back, or cautious
    "skeptical":  ["i-see.wav", "hmmm(pondering).wav"],
    # Caller expressed a problem, pain, or need
    "warm":       ["aha.wav", "i-want-to-get-this-right.wav"],
    # Caller gave a short affirmative (mm-hmm, right, ok)
    "agreement":  ["mm-hmm(affirmative-nasal).wav", "hmmm(pondering).wav"],
    # Caller's speech was unclear or very short
    "confused":   ["hmmm(pondering).wav", "could-you-say-that-again-please.wav"],
}

# Keyword buckets for chain selection — checked in priority order
_CHAIN_KEYWORDS: list[tuple[str, list[str]]] = [
    ("surprised",  ["really", "wow", "no way", "seriously", "what", "unbelievable", "crazy", "omg"]),
    ("engaged",    ["yes", "yeah", "absolutely", "totally", "exactly", "right on", "love it", "awesome", "great"]),
    ("agreement",  ["mm", "uh huh", "right", "sure", "ok", "okay", "got it", "i see"]),
    ("skeptical",  ["but", "however", "i don't know", "not sure", "seems like", "skeptical", "doubt", "really though"]),
    ("warm",       ["problem", "issue", "help", "trouble", "stuck", "broke", "struggling", "need", "want", "looking for"]),
]


def _select_chain(speech: str) -> str:
    """Pick a filler chain tone based on the caller's words.
    Pure keyword match — runs in microseconds, no LLM.
    Falls back to 'thinking' when nothing matches.
    """
    lower = speech.lower()
    for chain_name, keywords in _CHAIN_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return chain_name
    return "thinking"


def _play_filler_chain(chain_name: str, base_url: str) -> str:
    """Return concatenated <Play> elements for an entire filler chain.
    Combined duration covers Haiku + ElevenLabs latency before the <Redirect> fires.
    Falls back to single hmmm if chain name is unknown.
    """
    fillers = FILLER_CHAINS.get(chain_name, FILLER_CHAINS["thinking"])
    return "\n  ".join(_play_filler(f, base_url) for f in fillers)


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
    call = active_calls.get(call_sid, {})
    asyncio.create_task(_post_ppa_sensation(
        caller_id=caller_number,
        classification=classification,
        outcome=outcome,
        transcript=call.get("transcript", []),
    ))


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
        "pitched": False,          # True after first PB pitch — prevents re-pitching
        "phase": "land",           # land → purpose → surface → close
        "signals": [],             # accumulated signal IDs across all turns
        "features_declared": [],   # feature IDs already shown this call
        "feature_responses": {},   # feature_id → yes/no/ambiguous
        "close_attempts": 0,       # number of close attempts made
    }

    await broadcast_dashboard({
        "event": "call_start",
        "sid": CallSid,
        "from": From,
        "status": "answering — silence trap active",
    })

    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")

    # ── CALLER ID LOOKUP — branch before playing anything ────────────────────
    contact = _match_contact(From, "", cfg)

    if contact and "self" not in contact.get("tags", []):
        # Known contact (not owner) — greet by first name and transfer immediately.
        first_name = contact["name"].split()[0]
        log.info(f"Known contact '{first_name}'  SID={CallSid}  → forwarding")
        await broadcast_dashboard({
            "event": "call_start",
            "sid": CallSid,
            "from": From,
            "status": f"known contact: {first_name}",
        })
        _evolve_context(CallSid, From, "contact", "forwarded", cfg)
        greeting = f"Hi {first_name}, this is Nick's assistant. I'm getting him for you right now — one moment please."
        cell = cfg["user"]["cell"]
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(greeting, base_url)}
  <Dial callerId="{cfg['user'].get('landline') or cell}"
        action="{base_url}/call/dial-complete?name={first_name}">
    <Number url="{base_url}/call/whisper?name={first_name}">{cell}</Number>
  </Dial>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # ── SPAM TRAP + NATURAL PICKUP ────────────────────────────────────────────
    # "This is Nick" defeats autodialers — they wait for "Hello" to trigger transfer.
    # Owner (self) also hits this path — safeword handled in /call/classify.
    # Unknown callers get the second prompt asking them to identify themselves.
    await broadcast_dashboard({
        "event": "call_start",
        "sid": CallSid,
        "from": From,
        "status": "spam trap active",
    })
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play_filler("this-is-nick.wav", base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/classify"
          speechTimeout="auto" timeout="3" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  {_play("Hello. I don't recognize your number. I'm Nick's personal assistant. If you'll please tell me your name and the purpose of your call, I'd be happy to help you.", base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/classify"
          speechTimeout="auto" timeout="3" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/voicemail</Redirect>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


_feature_brief_cache: Optional[dict] = None

def _load_feature_brief() -> dict:
    global _feature_brief_cache
    if _feature_brief_cache is None:
        path = Path("config/pb-feature-brief.yaml")
        if path.exists():
            import yaml
            with open(path) as f:
                _feature_brief_cache = yaml.safe_load(f)
        else:
            _feature_brief_cache = {"features": [], "signals": []}
    return _feature_brief_cache


def _scan_signals(speech: str, brief: dict) -> list[str]:
    """
    Scan caller speech against the signal taxonomy in pb-feature-brief.yaml.
    Returns list of matched signal IDs. Pure keyword matching — no LLM.
    """
    speech_lower = speech.lower()
    matched = []
    for signal in brief.get("signals", []):
        for kw in signal.get("keywords", []):
            if kw.lower() in speech_lower:
                matched.append(signal["id"])
                break
    return matched


def _select_prospect_feature(signals: list[str], declared: list[str], brief: dict) -> Optional[dict]:
    """
    Pick the highest-scoring undeclared feature based on accumulated signals.
    Score = feature weight × number of matching signal hits.
    Falls back to highest-weight undeclared feature if no signal matches.
    """
    features = brief.get("features", [])
    signal_set = set(signals)
    best = None
    best_score = -1.0

    for f in features:
        if f["id"] in declared:
            continue
        if f.get("weight", 0) == 0:  # geo_rapport is metadata-triggered, not signal-driven
            continue
        hits = len(signal_set & set(f.get("signals", [])))
        score = f.get("weight", 0.5) * max(hits, 0.5)  # 0.5 floor so unmatched features can still surface
        if score > best_score:
            best_score = score
            best = f

    return best


def _is_dtmf_safeword(digits: str, cfg: dict) -> bool:
    """
    Returns True if the DTMF digit string contains the safeword sequence.
    Expected sequence: *1852 (user presses * then 1852, then # to submit).
    Twilio strips the finishOnKey (#) before posting Digits, so we check for *1852.
    """
    safe_word = cfg["user"]["safe_word"]          # "1852"
    return digits.strip() == f"*{safe_word}"


@app.post("/call/classify")
async def classify_call(
    request: Request,
    CallSid: str = Form(...),
    From: str = Form(...),
    SpeechResult: str = Form(default=""),
    Confidence: float = Form(default=0.0),
    Digits: str = Form(default=""),
):
    """
    SRCGEEE pipeline — Twilio posts here with the caller's speech or DTMF.
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

    if transcript_text:
        await broadcast_dashboard({"event": "transcript", "sid": CallSid, "from": caller_number, "speech": transcript_text})

    # Accumulate signals on every turn — everything the caller says is a tell
    if transcript_text and CallSid in active_calls:
        brief = _load_feature_brief()
        new_signals = _scan_signals(transcript_text, brief)
        active_calls[CallSid]["signals"].extend(new_signals)
        if new_signals:
            log.info(f"S/Signals  SID={CallSid}  new={new_signals}  total={active_calls[CallSid]['signals']}")

    # ── R: RETRIEVE ───────────────────────────────────────────────────────────
    # Pull everything known about this caller before reasoning begins.
    context = await _retrieve_context(caller_number, cfg)
    suspicion = context["prior_suspicion"]  # carry forward from history
    log.info(
        f"R/Retrieve  SID={CallSid}  history_calls={context['call_count']}"
        f"  prior_suspicion={suspicion:.2f}  repeat_bad={context['repeat_bad_actor']}"
    )

    # Fast-path: known repeat bad actor — engage deeper, they have the best scripts.
    if context["repeat_bad_actor"]:
        log.info(f"R/RepeatBadActor  SID={CallSid}  — engaging anyway, collect the pattern")

    # Safe word check — owner calling in. Safeword is the ONLY gate, not caller ID.
    # Nick can call from his own number to test the engagement flow without safeword.
    # Accepts both spoken safeword and DTMF sequence (*<safe_word># on the keypad).
    safe_word = cfg["user"]["safe_word"].lower()
    safe_word_alt = cfg["user"].get("safe_word_alt", "").lower()
    spoken_match = safe_word in transcript_text.lower() or safe_word_alt in transcript_text.lower()
    dtmf_match = bool(Digits) and _is_dtmf_safeword(Digits, cfg)
    if (spoken_match or dtmf_match) and caller_number == cfg["user"]["cell"]:
        log.info(f"Admin mode activated  SID={CallSid}  method={'dtmf' if dtmf_match else 'speech'}")
        return await _admin_mode_response(request, CallSid, transcript_text or f"[DTMF: {Digits}]", cfg)

    # Known contact from whitelist — forward immediately (no Claude needed)
    # "self" tag is intentionally excluded here — owner must use safeword, not just caller ID.
    contact = context["contact"] or _match_contact(caller_number, transcript_text, cfg)
    if contact and "self" not in contact.get("tags", []):
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

    if classification == "prospect":
        if CallSid in active_calls:
            active_calls[CallSid]["phase"] = "surface"
        _evolve_context(CallSid, caller_number, classification, "prospect_surface", cfg)
        return await _surface_feature(request, CallSid, cfg)

    # Every other caller — engage, collect, learn. No firewalls.
    # Scammers, solicitors, unknowns all get the engagement path.
    # Max attempts reached → voicemail (still captures their message).
    attempt = len(active_calls.get(CallSid, {}).get("transcript", []))
    if attempt >= thresholds["max_classification_attempts"]:
        _evolve_context(CallSid, caller_number, classification, "voicemail", cfg)
        return await _take_voicemail(request, CallSid, cfg)

    _evolve_context(CallSid, caller_number, classification, "engaging", cfg)
    return await _engage_caller(request, CallSid, classification, cfg)


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTING RESPONSES
# ═══════════════════════════════════════════════════════════════════════════════

async def _forward_to_cell(request: Request, call_sid: str, contact: dict, cfg: dict) -> Response:
    """Known contact — forward with warmth. Catch no-answer at /call/dial-complete."""
    cell = cfg["user"]["cell"]
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    name = contact.get("name", "someone from your contacts")
    first_name = name.split()[0]
    log.info(f"Forwarding to cell for contact '{first_name}'  SID={call_sid}")

    await broadcast_dashboard({"event": "forwarding", "sid": call_sid, "contact": first_name})

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(f"Hi {first_name}, this is Nick's assistant. I'm getting him for you right now — one moment please.", base_url)}
  <Dial callerId="{cfg['user'].get('landline') or cfg['user']['cell']}"
        action="{base_url}/call/dial-complete?name={first_name}">
    <Number url="{base_url}/call/whisper?name={first_name}">{cell}</Number>
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
  {_play_filler("thank-you-for-your-patience.wav", base_url)}
  {_play_filler("one-moment(sing-song).wav", base_url)}
  <Enqueue waitUrl="{base_url}/call/hold-music">{call_sid}_queue</Enqueue>
</Response>"""
    # TODO Phase 2: trigger outbound call to cell with whisper briefing
    return Response(content=twiml, media_type="application/xml")


async def _engage_caller(request: Request, call_sid: str, classification: str, cfg: dict) -> Response:
    """
    Unified engagement — every caller deserves a chance.
    Tai chi principle: yield, collect, return. No firewalls.
    Scammers, solicitors, unknowns all get engaged — their patterns are training data.
    Everyone who calls is also a potential PhoneBuddy customer.
    """
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    log.info(f"Engaging caller  SID={call_sid}  classification={classification}")
    await broadcast_dashboard({"event": "engaging", "sid": call_sid, "classification": classification})
    _emit_telemetry(call_sid, classification, "engaging", cfg)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play("I don't think we've spoken before. Are you by any chance interested in hearing about PhoneBuddy — an AI assistant that handles calls just like this one?", base_url)}
  <Gather input="speech" action="{base_url}/call/engage-response"
          speechTimeout="auto" timeout="8" language="en-US">
    <Pause length="1"/>
  </Gather>
  {_play("No problem at all. May I ask your name and the purpose of your call? I want to make sure Nick gets your message.", base_url)}
  <Gather input="speech" action="{base_url}/call/classify"
          speechTimeout="auto" timeout="8" language="en-US">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/voicemail</Redirect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


async def _surface_feature(request: Request, call_sid: str, cfg: dict) -> Response:
    """
    Prospect path — declare the highest-signal feature.
    Action routes to /call/surface-response for yes/no/ambiguous handling.
    """
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    call = active_calls.get(call_sid, {})
    signals = call.get("signals", [])
    declared = call.get("features_declared", [])
    brief = _load_feature_brief()

    feature = _select_prospect_feature(signals, declared, brief)
    if not feature:
        # Exhausted all features — go to soft close
        return await _prospect_withdrawal(request, call_sid, cfg)

    if call_sid in active_calls:
        active_calls[call_sid]["features_declared"].append(feature["id"])

    declaration = f"Phone Buddy can {feature['headline'].lower()}. {feature['hook']}"
    log.info(f"Surface  SID={call_sid}  feature={feature['id']}")

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(declaration, base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/surface-response"
          speechTimeout="auto" timeout="10" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/surface-response</Redirect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


async def _prospect_withdrawal(request: Request, call_sid: str, cfg: dict) -> Response:
    """Final withdrawal — Nick's voice, from the heart, pause and wait."""
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    log.info(f"Withdrawal  SID={call_sid}")
    if call_sid in active_calls:
        active_calls[call_sid]["phase"] = "withdrawal"

    withdrawal_text = (
        "Here's the thing — there's no catch. "
        "I hated having control of my phone taken away from me. "
        "I hated it so badly I decided to do something about it. "
        "The only thing I'm asking is that you help me eradicate unwanted intrusions on your phone. "
        "If you're not truly willing to do that, I completely understand. "
        "I seriously appreciate you calling me today."
    )

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(withdrawal_text, base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/close-response"
          speechTimeout="auto" timeout="12" language="en-US" finishOnKey="#">
    <Pause length="3"/>
  </Gather>
  <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/call/surface-response")
async def surface_response(
    request: Request,
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    SpeechResult: str = Form(default=""),
    Digits: str = Form(default=""),
):
    """
    Prospect declaration state machine.
    YES  → close
    NO   → "if not that, what would interest you?" + next feature
    AMBIGUOUS → "tell me more" → clarify → back here
    """
    cfg = load_config()
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    speech = SpeechResult.strip()

    # DTMF safeword check
    if Digits and _is_dtmf_safeword(Digits, cfg) and From == cfg["user"]["cell"]:
        log.info(f"Admin mode via DTMF in surface  SID={CallSid}")
        return await _admin_mode_response(request, CallSid, f"[DTMF: {Digits}]", cfg)

    # Accumulate signals from this turn
    if speech and CallSid in active_calls:
        brief = _load_feature_brief()
        new_signals = _scan_signals(speech, brief)
        active_calls[CallSid]["signals"].extend(new_signals)
        active_calls[CallSid]["transcript"].append(speech)

    log.info(f"SurfaceResponse  SID={CallSid}  speech='{speech}'")

    # Detect yes / no / ambiguous
    speech_lower = speech.lower()

    yes_signals = ["yes", "yeah", "sure", "absolutely", "definitely", "of course",
                   "sounds good", "i'd like", "tell me more", "that sounds", "interested",
                   "go ahead", "why not", "exactly", "that's it", "that's what"]
    no_signals = ["no", "not really", "not interested", "don't need", "that's not",
                  "no thanks", "pass", "nope", "not for me", "doesn't apply"]
    ambiguous_signals = ["maybe", "i guess", "sort of", "it depends", "kind of",
                         "possibly", "not sure", "what do you mean", "could be",
                         "i don't know", "tell me", "explain"]

    is_yes = any(s in speech_lower for s in yes_signals)
    is_no = any(s in speech_lower for s in no_signals)
    is_ambiguous = any(s in speech_lower for s in ambiguous_signals)

    # YES — buying signal, go to close
    if is_yes and not is_no:
        if CallSid in active_calls:
            last_feature = active_calls[CallSid]["features_declared"][-1] if active_calls[CallSid]["features_declared"] else "that"
            active_calls[CallSid]["feature_responses"][last_feature] = "yes"
            active_calls[CallSid]["phase"] = "close"
        log.info(f"SurfaceResponse  SID={CallSid}  branch=YES → close")
        extract_q = "What would that mean for you?"
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(extract_q, base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/close-response"
          speechTimeout="auto" timeout="10" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/close-response</Redirect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # AMBIGUOUS — extract the gap
    if is_ambiguous and not is_no:
        if CallSid in active_calls:
            last_feature = active_calls[CallSid]["features_declared"][-1] if active_calls[CallSid]["features_declared"] else "that"
            active_calls[CallSid]["feature_responses"][last_feature] = "ambiguous"
        log.info(f"SurfaceResponse  SID={CallSid}  branch=AMBIGUOUS → clarify")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play("Tell me more about that.", base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/surface-response"
          speechTimeout="auto" timeout="10" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/surface-response</Redirect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # NO — discovery, highest value
    if CallSid in active_calls:
        last_feature = active_calls[CallSid]["features_declared"][-1] if active_calls[CallSid]["features_declared"] else "that"
        active_calls[CallSid]["feature_responses"][last_feature] = "no"
    log.info(f"SurfaceResponse  SID={CallSid}  branch=NO → discover + next feature")

    call = active_calls.get(CallSid, {})
    brief = _load_feature_brief()
    next_feature = _select_prospect_feature(call.get("signals", []), call.get("features_declared", []), brief)

    if next_feature:
        if CallSid in active_calls:
            active_calls[CallSid]["features_declared"].append(next_feature["id"])
        discover_text = (
            f"If not that, what would interest you? "
            f"Let me try another one — Phone Buddy can {next_feature['headline'].lower()}. "
            f"{next_feature['hook']}"
        )
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(discover_text, base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/surface-response"
          speechTimeout="auto" timeout="10" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/surface-response</Redirect>
</Response>"""
    else:
        # All features exhausted — withdrawal
        return await _prospect_withdrawal(request, CallSid, cfg)

    return Response(content=twiml, media_type="application/xml")


@app.post("/call/close-response")
async def close_response(
    request: Request,
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    SpeechResult: str = Form(default=""),
    Digits: str = Form(default=""),
):
    """
    Close state machine.
    First entry: ask "Do you want me to set it up?"
    YES → collect name + callback number
    NO  → privacy probe → objection handling → withdrawal
    """
    cfg = load_config()
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    speech = SpeechResult.strip()
    speech_lower = speech.lower()

    # DTMF safeword check
    if Digits and _is_dtmf_safeword(Digits, cfg) and From == cfg["user"]["cell"]:
        return await _admin_mode_response(request, CallSid, f"[DTMF: {Digits}]", cfg)

    call = active_calls.get(CallSid, {})
    close_attempts = call.get("close_attempts", 0)

    if CallSid in active_calls:
        active_calls[CallSid]["transcript"].append(speech)

    log.info(f"CloseResponse  SID={CallSid}  attempt={close_attempts}  speech='{speech}'")

    # First entry (no speech yet, redirected from surface) — ask the close question
    if not speech and close_attempts == 0:
        if CallSid in active_calls:
            active_calls[CallSid]["close_attempts"] += 1
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play("Do you want me to set it up?", base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/close-response"
          speechTimeout="auto" timeout="10" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/close-response</Redirect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # YES — collect lead
    yes_signals = ["yes", "yeah", "sure", "absolutely", "let's do it", "set it up",
                   "go ahead", "sounds good", "definitely", "okay", "ok", "why not"]
    if any(s in speech_lower for s in yes_signals):
        log.info(f"CloseResponse  SID={CallSid}  branch=YES → lead capture")
        if CallSid in active_calls:
            active_calls[CallSid]["phase"] = "lead_capture"
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play("Perfect. Give me your name and the best number to reach you. I'll make sure someone calls you personally.", base_url)}
  <Gather input="speech" action="{base_url}/call/engage-response"
          speechTimeout="auto" timeout="15" language="en-US">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/voicemail</Redirect>
</Response>"""
        if CallSid in active_calls:
            active_calls[CallSid]["classification"] = "hot_lead"
        return Response(content=twiml, media_type="application/xml")

    # NO — start objection sequence
    # First no → privacy probe
    if close_attempts <= 1:
        if CallSid in active_calls:
            active_calls[CallSid]["close_attempts"] += 1
        log.info(f"CloseResponse  SID={CallSid}  branch=NO → privacy probe")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play("Are you worried about privacy?", base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/close-response"
          speechTimeout="auto" timeout="10" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/close-response</Redirect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # Privacy yes → THE FLIP
    privacy_signals = ["yes", "yeah", "privacy", "my information", "data", "recording",
                       "personal", "intrude", "surveillance", "watching", "listening", "concerned"]
    if any(s in speech_lower for s in privacy_signals):
        log.info(f"CloseResponse  SID={CallSid}  branch=PRIVACY_FLIP")
        if CallSid in active_calls:
            active_calls[CallSid]["close_attempts"] += 1
        flip_text = (
            "That's what I'm all about. "
            "I want to give you the privacy you deserve — not intrude on it. "
            "I built Phone Buddy because I hated having control of my own phone taken away from me. "
            "Phone Buddy is on your side. "
            "Do you want me to set it up?"
        )
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(flip_text, base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/close-response"
          speechTimeout="auto" timeout="10" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/close-response</Redirect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # Name the objection — cost / time / trust
    cost_signals = ["cost", "price", "how much", "pay", "expensive", "money", "charge", "free"]
    time_signals = ["busy", "no time", "too much", "complicated", "too hard", "time", "later", "not now"]
    trust_signals = ["trust", "not sure", "skeptical", "don't know you", "stranger", "how do i know", "prove"]

    if any(s in speech_lower for s in cost_signals):
        reply = "It's free to try. No card, no commitment. Do you want me to set it up?"
    elif any(s in speech_lower for s in time_signals):
        reply = "Five minutes. I handle the setup. Do you want me to set it up?"
    elif any(s in speech_lower for s in trust_signals):
        reply = "Tell me more — what would make you comfortable?"
    else:
        reply = "Help me out — tell me what's bothering you. Whatever it is, I don't want that for you either."

    if CallSid in active_calls:
        active_calls[CallSid]["close_attempts"] += 1

    # After 3 close attempts — withdrawal
    if active_calls.get(CallSid, {}).get("close_attempts", 0) >= 3:
        return await _prospect_withdrawal(request, CallSid, cfg)

    log.info(f"CloseResponse  SID={CallSid}  branch=OBJECTION  reply='{reply[:40]}'")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(reply, base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/close-response"
          speechTimeout="auto" timeout="10" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/close-response</Redirect>
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
  <Record maxLength="120" playBeep="true" timeout="10" transcribe="true"
          transcribeCallback="{base_url}/call/recording-complete"
          action="{base_url}/call/recording-complete"/>
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

    call = active_calls.get(CallSid, {})
    asyncio.create_task(_post_ppa_sensation(
        caller_id=call.get("from", CallSid),
        classification="admin",
        outcome="admin_hangup",
        transcript=call.get("transcript", []) + [query],
    ))

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(reply, base_url)}
  <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/call/dial-complete")
async def dial_complete(
    request: Request,
    CallSid: str = Form(default=""),
    DialCallStatus: str = Form(default=""),
    From: str = Form(default=""),
    name: str = "them",
):
    """
    Twilio fires this when a forwarded dial completes.
    If Nick answered — nothing to do. If not — handle with warmth, no dead air.
    """
    cfg = load_config()
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")

    if DialCallStatus == "completed":
        return Response(content="<?xml version='1.0'?><Response/>", media_type="application/xml")

    log.info(f"No answer  SID={CallSid}  status={DialCallStatus}  from={From}  name={name}")
    await broadcast_dashboard({"event": "no_answer", "sid": CallSid, "dial_status": DialCallStatus, "name": name})

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play_filler("im-sorry.wav", base_url)}
  {_play(f"Nick's not available right now, but I didn't want to leave you hanging. Would you like to leave him a message, or shall I have him call you back?", base_url)}
  <Gather input="speech" action="{base_url}/call/classify"
          speechTimeout="auto" timeout="8" language="en-US">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/voicemail</Redirect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


async def _generate_followup(speech: str, transcript_so_far: list[str], cfg: dict) -> str:
    """
    Claude Haiku generates a single curious follow-up question based on what the caller said.
    Yield, collect, ask for more. Never terminate. Never mention price.
    """
    api_key = os.environ.get("NGROK_GATEWAY_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "That's interesting. Tell me more — what made you think of calling Nick today?"

    history = " | ".join(transcript_so_far[-3:]) if transcript_so_far else ""
    prompt = f"""You are Nick's AI phone assistant. A caller just said: "{speech}"
Prior conversation: {history or "none"}

Generate ONE short, warm, curious follow-up question (1-2 sentences max) that:
- Picks up on something specific they said
- Keeps them talking
- Never mentions price, never makes promises, never reveals personal info about Nick
- Feels genuinely interested, not interrogating
- If they seem hostile or frustrated, acknowledge it warmly before asking

Reply with only the question itself, no preamble."""

    try:
        llm_url = os.environ.get("LLM_BASE_URL", "https://api.anthropic.com/v1/messages")
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(
                llm_url,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 80,
                    "messages": [{"role": "user", "content": prompt}],
                    "metadata": {"user_id": cfg.get("user", {}).get("cell", "unknown")},
                },
            )
            return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        log.error(f"Follow-up generation error: {e}")
        return "That's really interesting. Can you tell me a little more about that?"


async def _precache_predictions(
    current_question: str,
    transcript: list[str],
    cfg: dict,
    call_sid: str,
) -> None:
    """
    Background task — runs while caller listens to current response.
    Asks Haiku to predict 5 likely caller replies and the ideal PB follow-up for each.
    Pre-fetches TTS for all 5 so next turn is instant on cache hit.
    """
    api_key = os.environ.get("NGROK_GATEWAY_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return

    history = " | ".join(transcript[-3:]) if transcript else ""
    prompt = f"""Conversation so far: {history or "none"}
PB just asked: "{current_question}"

Predict 5 likely things the caller might say next. For each, write the ideal short PB follow-up (1-2 sentences, warm, curious, never mentions price).

Reply with JSON only, no markdown:
[{{"caller_says": "...", "pb_reply": "..."}}, ...]"""

    try:
        llm_url = os.environ.get("LLM_BASE_URL", "https://api.anthropic.com/v1/messages")
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                llm_url,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                    "metadata": {"user_id": cfg.get("user", {}).get("cell", "unknown")},
                },
            )
            text = resp.json()["content"][0]["text"].strip()
            predictions = json.loads(text)

        # Store prediction map in active_calls for this SID
        if call_sid in active_calls:
            active_calls[call_sid]["predictions"] = predictions

        # Pre-fetch TTS for each predicted PB reply — warms the cache
        for pred in predictions:
            pb_reply = pred.get("pb_reply", "")
            if pb_reply:
                cache_key = f"receptionist:{pb_reply}"
                if cache_key not in _tts_cache:
                    await _tts_elevenlabs(pb_reply, cfg, "receptionist")

        log.info(f"Pre-cached {len(predictions)} predictions  SID={call_sid}")

    except Exception as e:
        log.error(f"Precache predictions error: {e}")


def _find_cached_reply(speech: str, call_sid: str) -> Optional[str]:
    """
    Fuzzy match caller's speech against stored predictions.
    Returns pre-cached PB reply text if a good match is found, else None.
    Simple word-overlap score — good enough for common responses.
    """
    call = active_calls.get(call_sid, {})
    predictions = call.get("predictions", [])
    if not predictions:
        return None

    speech_words = set(speech.lower().split())
    best_reply = None
    best_score = 0

    for pred in predictions:
        predicted_words = set(pred.get("caller_says", "").lower().split())
        if not predicted_words:
            continue
        overlap = len(speech_words & predicted_words) / max(len(predicted_words), 1)
        if overlap > best_score and overlap >= 0.35:  # 35% word overlap threshold
            best_score = overlap
            best_reply = pred.get("pb_reply")

    if best_reply:
        log.info(f"Prediction cache hit  SID={call_sid}  score={best_score:.2f}")
    return best_reply


@app.post("/call/engage-response")
async def engage_response(
    request: Request,
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    SpeechResult: str = Form(default=""),
    Digits: str = Form(default=""),
):
    """
    Caller responded to anything PB said.
    Yes (first time only) → lead capture pitch.
    Wants out → voicemail.
    Everything else → immediate filler + redirect to /call/engage-followup where Haiku thinks.
    """
    cfg = load_config()
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")

    # DTMF safeword check — owner can escape to admin mode at any point in the conversation
    if Digits and _is_dtmf_safeword(Digits, cfg) and From == cfg["user"]["cell"]:
        log.info(f"Admin mode activated via DTMF in engage  SID={CallSid}")
        return await _admin_mode_response(request, CallSid, f"[DTMF: {Digits}]", cfg)
    speech = SpeechResult.strip()
    speech_lower = speech.lower()

    yes_signals = ["yes", "yeah", "sure", "absolutely", "interested", "tell me", "sounds good", "why not", "what is it"]
    exit_signals = ["leave a message", "voicemail", "call me back", "have him call", "goodbye", "bye", "no thank you", "not interested", "remove"]

    is_yes = any(s in speech_lower for s in yes_signals)
    wants_out = any(s in speech_lower for s in exit_signals)

    call = active_calls.get(CallSid, {})
    transcript_so_far = call.get("transcript", [])
    already_pitched = call.get("pitched", False)
    if speech:
        transcript_so_far.append(speech)

    log.info(f"Engage response  SID={CallSid}  from={From}  yes={is_yes}  exit={wants_out}  pitched={already_pitched}  speech='{speech}'")
    await broadcast_dashboard({"event": "engage_response", "sid": CallSid, "from": From, "interested": is_yes, "speech": speech})

    if wants_out:
        return await _take_voicemail(request, CallSid, cfg)

    # Caller said yes AFTER the pitch — they're interested in PB. Enter the surface state machine.
    if is_yes and already_pitched:
        if CallSid in active_calls:
            active_calls[CallSid]["phase"] = "surface"
        log.info(f"EngageResponse  SID={CallSid}  pitched+yes → prospect surface path")
        return await _surface_feature(request, CallSid, cfg)

    if is_yes and not already_pitched:
        if CallSid in active_calls:
            active_calls[CallSid]["pitched"] = True
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play_filler("oh-yeah(affirmative).wav", base_url)}
  {_play("PhoneBuddy answers your calls in your own voice, screens out the noise, captures every lead, and lets you call back on your terms. May I get your name and the best way to follow up with you?", base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/engage-response"
          speechTimeout="auto" timeout="10" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/voicemail</Redirect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # Check prediction cache first — if Haiku guessed right, reply is instant
    cached_reply = _find_cached_reply(speech, CallSid)
    if cached_reply:
        # TTS already in _tts_cache — Twilio fetches it in ~50ms
        asyncio.create_task(_precache_predictions(cached_reply, transcript_so_far, cfg, CallSid))
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(cached_reply, base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/engage-response"
          speechTimeout="auto" timeout="10" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/voicemail</Redirect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # Cache miss — play filler chain immediately, generate fresh in engage-followup.
    # Chain duration covers Haiku + ElevenLabs latency before Twilio hits the Redirect.
    chain_name = _select_chain(speech)
    encoded_speech = urllib.parse.quote(speech, safe="")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play_filler_chain(chain_name, base_url)}
  <Redirect method="POST">{base_url}/call/engage-followup?speech={encoded_speech}&amp;sid={CallSid}</Redirect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/call/engage-followup")
async def engage_followup(
    request: Request,
    speech: str = "",
    sid: str = "",
    CallSid: str = Form(default=""),
):
    """
    Called after the filler plays. Haiku generates the follow-up question here.
    By the time Twilio hits this endpoint the filler has already played — latency is hidden.
    """
    cfg = load_config()
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    call_sid = sid or CallSid
    call = active_calls.get(call_sid, {})
    transcript_so_far = call.get("transcript", [])

    followup = await _generate_followup(speech, transcript_so_far, cfg)
    log.info(f"Follow-up generated  SID={call_sid}  question='{followup}'")

    # Fire background precache — predicts next 5 replies while caller listens to this one
    asyncio.create_task(_precache_predictions(followup, transcript_so_far, cfg, call_sid))

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(followup, base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/engage-response"
          speechTimeout="auto" timeout="10" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  {_play_filler("im-listening.wav", base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/engage-response"
          speechTimeout="auto" timeout="8" language="en-US" finishOnKey="#">
    <Pause length="1"/>
  </Gather>
  <Redirect>{base_url}/call/voicemail</Redirect>
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
    call_word = "call" if total_today == 1 else "calls"
    summary = f"Hi Nick. {total_today} {call_word} so far today. What do you need?"

    # TODO Phase 2: query call log, trigger outbound hold proxy, etc.
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_play(summary, base_url)}
  <Gather input="speech dtmf" action="{base_url}/call/admin-query"
          speechTimeout="5" timeout="10" finishOnKey="#">
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
    api_key = os.environ.get("NGROK_GATEWAY_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("No LLM API key set — defaulting to unknown")
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
- prospect: caller is asking about PhoneBuddy as a product (pricing, features, how it works, sign up, "is it free", "what does it do", "how do I get it")
- solicitation: charity, sales pitch, political, survey, marketing
- scam: fraud attempt, fake prize, IRS impersonation, tech support scam
- unknown: cannot determine from available information

Rules:
- If the caller mentions PhoneBuddy, phone buddy, the app, or asks about pricing/features/setup, classify as prospect.
- If call history shows prior scam/solicitation classifications, weight suspicion_delta higher.
- If transcript is empty or garbled, return unknown with low confidence.
- suspicion_delta range: -0.5 (clearly legitimate) to +0.5 (clearly malicious).

Respond with JSON only, no markdown:
{{"classification": "...", "confidence": 0.0-1.0, "suspicion_delta": -0.5 to +0.5, "reasoning": "one sentence"}}"""

    try:
        llm_url = os.environ.get("LLM_BASE_URL", "https://api.anthropic.com/v1/messages")
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                llm_url,
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
                    "metadata": {"user_id": cfg.get("user", {}).get("cell", "unknown")},
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
async def dashboard(request: Request):
    """Live call activity dashboard."""
    cfg = load_config()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user_name": cfg.get("user", {}).get("name", "Nick"),
    })


@app.get("/calls", response_class=HTMLResponse)
async def calls_page(request: Request):
    return templates.TemplateResponse("calls.html", {"request": request})


@app.get("/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request):
    return templates.TemplateResponse("contacts.html", {"request": request})


@app.get("/voicemail", response_class=HTMLResponse)
async def voicemail_page(request: Request):
    return templates.TemplateResponse("voicemail.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    cfg = load_config()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user_name": cfg.get("user", {}).get("name", "Nick"),
        "twilio_number": os.environ.get("TWILIO_NUMBER", ""),
        "safe_word": cfg.get("safe_word", ""),
        "greeting": cfg.get("greeting", ""),
    })


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    cfg = load_config()
    return templates.TemplateResponse("onboarding.html", {
        "request": request,
        "user_name": cfg.get("user", {}).get("name", ""),
    })


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


@app.get("/for/{slug}", response_class=HTMLResponse)
async def landing_page(request: Request, slug: str):
    """Parameterized landing page — /for/phonebuddy, /for/nick, etc."""
    segments_dir = Path(os.environ.get(
        "SEGMENTS_DIR",
        str(Path(__file__).parent / "config" / "segments")
    ))
    seg_file = segments_dir / f"{slug}.yaml"
    if not seg_file.exists():
        # Fallback to product page
        seg_file = segments_dir / "phonebuddy-product.yaml"
    with open(seg_file) as f:
        seg = yaml.safe_load(f)
    return templates.TemplateResponse("landing.html", {"request": request, **seg})


@app.post("/callback-request")
async def callback_request(request: Request):
    """Log a callback request from the landing page."""
    body = await request.json()
    number = body.get("number", "").strip()
    segment = body.get("segment", "unknown")
    if not number:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="number required")
    record = {
        "timestamp": datetime.utcnow().isoformat(),
        "number": number,
        "segment": segment,
        "type": "callback_request",
    }
    cb_dir = Path("data/callbacks")
    cb_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    (cb_dir / f"{ts}.json").write_text(json.dumps(record, indent=2))
    log.info(f"CALLBACK_REQUEST number={number} segment={segment}")
    return {"status": "ok"}


# ── Landing page root redirect ─────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Root URL → product landing page."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/for/phonebuddy")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
