"""
Refactored inbound call handler — Two-Pass R Architecture

Flow:
  T+0ms     Twilio webhook fires → build PBSensation (S)
  T+50ms    R1: CRM lookup only (caller name, known/unknown)
  T+100ms   PB starts greeting audio (buys 1500ms)
  T+100ms+  R2: async full profile, history, predictions, SMBs
  T+1500ms  Caller speaks → PPA has full context ready

This is a FastAPI router.  Mount it in main.py when ready:
    from inbound_v2 import router as inbound_v2_router
    app.include_router(inbound_v2_router)

The original /call/inbound stays live until this is proven.
"""

import asyncio
import logging
from datetime import datetime

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from contracts import (
    CallerKnowledge,
    ConversationPhase,
    PBSensation,
    PPAResponse,
    R1Result,
    R2Result,
    ResponseMode,
    TwilioGeo,
    VerificationState,
)

log = logging.getLogger("phonebuddy")

router = APIRouter(prefix="/v2", tags=["inbound-v2"])

# ---------------------------------------------------------------------------
# In-memory call state (same pattern as main.py active_calls, will unify later)
# ---------------------------------------------------------------------------
_active: dict[str, dict] = {}


# ===========================================================================
# S PHASE — Build Sensation from Twilio webhook
# ===========================================================================

def _build_ring_sensation(
    call_sid: str,
    caller: str,
    called: str,
    status: str,
    form_data: dict,
) -> PBSensation:
    """
    Package raw Twilio webhook fields into a typed Sensation.
    PB classifies nothing — it just captures what Twilio provides.
    """
    geo = TwilioGeo(
        city=form_data.get("FromCity"),
        state=form_data.get("FromState"),
        zip=form_data.get("FromZip"),
        country=form_data.get("FromCountry"),
        caller_name=form_data.get("CallerName"),
    )
    return PBSensation(
        call_sid=call_sid,
        caller_phone=caller,
        called_phone=called,
        event="ring",
        geo=geo,
        call_status=status,
    )


# ===========================================================================
# R1 PHASE — Fast CRM lookup (< 50ms, deterministic, $0)
# ===========================================================================

async def _r1_lookup(sensation: PBSensation, ppa_url: str) -> R1Result:
    """
    R Phase 1: ask PPA for a fast caller lookup.
    PPA queries the caller table by phone number.  Returns name + tags + call_count.

    If PPA is unreachable, degrade gracefully to UNKNOWN.
    """
    if not ppa_url:
        return R1Result(
            caller_phone=sensation.caller_phone,
            caller_knowledge=CallerKnowledge.UNKNOWN,
            geo=sensation.geo,
        )

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(
                f"{ppa_url}/v1/r1-lookup",
                json={
                    "caller_phone": sensation.caller_phone,
                    "geo": sensation.geo.model_dump(),
                },
            )
            if resp.status_code == 200:
                return R1Result(**resp.json())
    except Exception as exc:
        log.warning(f"R1 lookup failed, degrading to UNKNOWN: {exc}")

    return R1Result(
        caller_phone=sensation.caller_phone,
        caller_knowledge=CallerKnowledge.UNKNOWN,
        geo=sensation.geo,
    )


# ===========================================================================
# R2 PHASE — Full profile retrieval (async, runs during greeting)
# ===========================================================================

async def _r2_full_profile(
    sensation: PBSensation,
    r1: R1Result,
    ppa_url: str,
) -> R2Result:
    """
    R Phase 2: full profile retrieval.  Runs async while PB plays the greeting.
    Queries: call history, predicted sensations, nearest SMBs, full profile.

    Result is stored in _active[call_sid]["r2"] when complete.
    """
    if not ppa_url or not r1.caller_id:
        return R2Result(r1=r1, ready=True)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{ppa_url}/v1/r2-profile",
                json={
                    "caller_id": str(r1.caller_id),
                    "caller_phone": sensation.caller_phone,
                    "call_sid": sensation.call_sid,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                data["r1"] = r1.model_dump()
                return R2Result(**data)
    except Exception as exc:
        log.warning(f"R2 profile failed, proceeding with R1 only: {exc}")

    return R2Result(r1=r1, ready=True)


async def _r2_background(sensation: PBSensation, r1: R1Result, ppa_url: str):
    """Fire-and-forget R2.  Stores result in _active when done."""
    r2 = await _r2_full_profile(sensation, r1, ppa_url)
    if sensation.call_sid in _active:
        _active[sensation.call_sid]["r2"] = r2
        log.info(f"R2 ready  SID={sensation.call_sid}  predictions={len(r2.predictions)}")


# ===========================================================================
# GREETING SELECTION (uses R1 to pick the right opening)
# ===========================================================================

def _select_greeting(r1: R1Result, base_url: str, cfg: dict) -> tuple[str, str]:
    """
    Pick greeting TwiML based on R1 result.  Returns (twiml_fragment, log_msg).

    Known caller:       "Hey {name}, what's up?" (buys ~1500ms for R2)
    Known + self tag:   Safeword path (owner calling)
    Unknown caller:     Spam trap: "This is Nick" then gather
    """
    if r1.caller_knowledge == CallerKnowledge.SELF:
        # Owner calling — play safeword prompt (unchanged from v1)
        return (
            _tts(f"Hey, it's your assistant. What's the word?", base_url),
            "owner calling — safeword path",
        )

    if r1.caller_knowledge == CallerKnowledge.KNOWN:
        first_name = (r1.caller_name or "there").split()[0]
        # Warm greeting that buys R2 time
        # "do you want Nick, or can I help?" is the triage gate
        greeting = (
            f"Hey {first_name}! This is Nick's assistant. "
            f"Do you want me to get Nick, or is there something I can help you with?"
        )
        return (
            _tts(greeting, base_url),
            f"known contact: {first_name} (call #{r1.call_count + 1})",
        )

    # Unknown caller — spam trap defeats autodialers
    return (
        _play_static("this-is-nick.wav", base_url),
        "unknown caller — spam trap active",
    )


# ===========================================================================
# TwiML HELPERS
# ===========================================================================

def _tts(text: str, base_url: str) -> str:
    """Generate a <Say> or <Play> TwiML fragment for text.
    TODO: swap to ElevenLabs TTS endpoint when ready.
    """
    # For now, use Twilio's built-in <Say>.  ElevenLabs integration is a separate PR.
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    return f'<Say voice="Polly.Matthew" language="en-US">{safe}</Say>'


def _play_static(filename: str, base_url: str) -> str:
    return f'<Play>{base_url}/static/audio/fillers/{filename}</Play>'


def _gather(inner_twiml: str, base_url: str, action_path: str) -> str:
    return (
        f'<Gather input="speech dtmf" action="{base_url}{action_path}" '
        f'speechTimeout="auto" timeout="4" language="en-US" finishOnKey="#">'
        f"{inner_twiml}"
        f"</Gather>"
    )


# ===========================================================================
# INBOUND HANDLER — the new two-pass R architecture
# ===========================================================================

@router.post("/call/inbound")
async def inbound_call_v2(
    request: Request,
    CallSid: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
    CallStatus: str = Form(default="ringing"),
    # Geo fields — Twilio sends these for free, capture them all
    FromCity: str = Form(default=None),
    FromState: str = Form(default=None),
    FromZip: str = Form(default=None),
    FromCountry: str = Form(default=None),
    CallerName: str = Form(default=None),
):
    """
    /v2/call/inbound — Two-Pass R inbound handler.

    S:  Package Twilio fields into PBSensation
    R1: Fast CRM lookup (< 50ms) → picks greeting
    Greeting plays (buys 1500ms)
    R2: Full profile loads async (history, predictions, SMBs)
    Caller speaks → PPA has full context

    The v1 handler at /call/inbound stays live.  Point Twilio here when ready.
    """
    # Import from main.py — these will be unified later
    from main import (
        PUBLIC_URL,
        PPA_URL,
        active_calls,
        broadcast_dashboard,
        load_config,
    )

    cfg = load_config()
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")

    log.info(f"[v2] Inbound: {From} → {To}  SID={CallSid}")

    # ── S PHASE: Build Sensation ────────────────────────────────────────────
    form_data = dict(await request.form())
    sensation = _build_ring_sensation(CallSid, From, To, CallStatus, form_data)

    # ── R1 PHASE: Fast CRM lookup ──────────────────────────────────────────
    r1 = await _r1_lookup(sensation, PPA_URL)

    # ── Initialize call state ───────────────────────────────────────────────
    _active[CallSid] = {
        "sensation": sensation,
        "r1": r1,
        "r2": None,         # populated async
        "verification": VerificationState.NOT_NEEDED,
        "started": datetime.utcnow().isoformat(),
    }

    # Also register in main.py's active_calls for dashboard compat
    active_calls[CallSid] = {
        "sid": CallSid,
        "from": From,
        "to": To,
        "started": datetime.utcnow().isoformat(),
        "status": "answering",
        "transcript": [],
        "classification": r1.caller_knowledge.value,
        "suspicion_score": 0.0,
        "pitched": False,
        "phase": "greeting",
        "signals": [],
        "features_declared": [],
        "feature_responses": {},
        "close_attempts": 0,
    }

    await broadcast_dashboard({
        "event": "call_start",
        "sid": CallSid,
        "from": From,
        "caller_name": r1.caller_name,
        "caller_knowledge": r1.caller_knowledge.value,
        "status": f"R1: {r1.caller_knowledge.value}",
    })

    # ── R2 PHASE: Fire async full profile (runs during greeting) ────────────
    if r1.caller_id:
        asyncio.create_task(_r2_background(sensation, r1, PPA_URL))

    # ── GREETING: Based on R1 result ────────────────────────────────────────
    greeting_twiml, log_msg = _select_greeting(r1, base_url, cfg)
    log.info(f"[v2] Greeting: {log_msg}  SID={CallSid}")

    # ── BUILD TWIML RESPONSE ────────────────────────────────────────────────
    if r1.caller_knowledge == CallerKnowledge.SELF:
        # Owner: safeword gather → /call/classify (reuse v1 for now)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_gather(greeting_twiml, base_url, "/call/classify")}
</Response>"""

    elif r1.caller_knowledge == CallerKnowledge.KNOWN:
        # Known caller: warm greeting with triage question
        # Response goes to /v2/call/triage where we parse intent
        _active[CallSid]["verification"] = VerificationState.UNVERIFIED
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_gather(greeting_twiml, base_url, "/v2/call/triage")}
  <Redirect>{base_url}/v2/call/triage?timeout=true</Redirect>
</Response>"""

    else:
        # Unknown: spam trap → identify yourself → /v2/call/identify
        identify_prompt = _tts(
            "Hello. I don't recognize your number. I'm Nick's personal assistant. "
            "If you'll please tell me your name and the purpose of your call, "
            "I'd be happy to help you.",
            base_url,
        )
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {greeting_twiml}
  {_gather('<Pause length="1"/>', base_url, "/v2/call/identify")}
  {identify_prompt}
  {_gather('<Pause length="1"/>', base_url, "/v2/call/identify")}
  <Redirect>{base_url}/call/voicemail</Redirect>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


# ===========================================================================
# TRIAGE — Known caller responded to "want Nick, or can I help?"
# ===========================================================================

@router.post("/call/triage")
async def triage_known_caller(
    request: Request,
    CallSid: str = Form(...),
    SpeechResult: str = Form(default=""),
):
    """
    Known caller responded to the greeting.  Three paths:

    1. "Get Nick" / "talk to Nick" → transfer
    2. Identity correction: "this is Roy" → re-identify, update context
    3. Anything else → PPA handles the conversation
    """
    from main import PUBLIC_URL, PPA_URL, broadcast_dashboard, load_config

    cfg = load_config()
    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    speech = SpeechResult.strip().lower()
    call = _active.get(CallSid, {})
    r1: R1Result | None = call.get("r1")

    log.info(f"[v2] Triage  SID={CallSid}  speech={speech!r}")

    if not speech:
        # Timeout — no response.  Ask again or route to voicemail.
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_gather(_tts("Are you still there?", base_url), base_url, "/v2/call/triage")}
  <Redirect>{base_url}/call/voicemail</Redirect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # ── Identity correction check ───────────────────────────────────────────
    # "No, this is Roy" / "This is Roy" / "I'm Roy"
    verification = _parse_verification(speech, r1.caller_name if r1 else None)
    if verification and verification["state"] != VerificationState.CONFIRMED:
        # Caller is NOT who we thought — update context
        if CallSid in _active:
            _active[CallSid]["verification"] = verification["state"]
            if verification.get("alt_name"):
                _active[CallSid]["actual_name"] = verification["alt_name"]

        alt = verification.get("alt_name", "friend")
        log.info(f"[v2] Identity correction: expected={r1.caller_name}, actual={alt}  SID={CallSid}")

        await broadcast_dashboard({
            "event": "identity_correction",
            "sid": CallSid,
            "expected": r1.caller_name if r1 else "unknown",
            "actual": alt,
        })

        # Re-greet with corrected name
        greeting = f"Oh hi {alt}! Sorry about that. Do you want me to get Nick, or can I help you with something?"
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_gather(_tts(greeting, base_url), base_url, "/v2/call/triage")}
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # ── Transfer request check ──────────────────────────────────────────────
    transfer_signals = ["get nick", "talk to nick", "nick please", "transfer", "put him on", "yes get him"]
    if any(sig in speech for sig in transfer_signals):
        cell = cfg["user"]["cell"]
        name = _active.get(CallSid, {}).get("actual_name") or (r1.caller_name if r1 else "caller")
        first = name.split()[0]
        log.info(f"[v2] Transfer requested by {first}  SID={CallSid}")

        transfer_msg = _tts(f"Sure thing! Let me get Nick for you. One moment please.", base_url)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {transfer_msg}
  <Dial callerId="{cfg['user'].get('landline') or cell}"
        action="{base_url}/call/dial-complete?name={first}">
    <Number url="{base_url}/call/whisper?name={first}">{cell}</Number>
  </Dial>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # ── Everything else → PPA conversation loop ─────────────────────────────
    # Build sensation from the utterance and send to PPA
    # For now, fall back to v1 classify endpoint
    # TODO: replace with PPA sensation POST when ppa-api has the endpoint
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Redirect method="POST">{base_url}/call/classify?SpeechResult={speech}</Redirect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# ===========================================================================
# IDENTIFY — Unknown caller told us who they are
# ===========================================================================

@router.post("/call/identify")
async def identify_unknown_caller(
    request: Request,
    CallSid: str = Form(...),
    SpeechResult: str = Form(default=""),
):
    """
    Unknown caller responded to "tell me your name and purpose."
    This is the S→R boundary for unknown callers.

    We send the full utterance to PPA as a Sensation.  PPA classifies intent
    and returns a PPAResponse with the next action.

    For now, falls back to v1 /call/classify.
    """
    from main import PUBLIC_URL

    base_url = PUBLIC_URL or str(request.base_url).rstrip("/")
    speech = SpeechResult.strip()

    log.info(f"[v2] Identify  SID={CallSid}  speech={speech!r}")

    if not speech:
        # No speech — redirect to voicemail
        return Response(
            content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Redirect>{base_url}/call/voicemail</Redirect></Response>',
            media_type="application/xml",
        )

    # TODO: Send PBSensation to PPA instead of falling back to v1
    # For now, reuse the v1 classify endpoint
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Redirect method="POST">{base_url}/call/classify</Redirect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# ===========================================================================
# VERIFICATION PARSER — "No, this is Roy" → {state, alt_name, confidence}
# ===========================================================================

def _parse_verification(speech: str, expected_name: str | None) -> dict | None:
    """
    Parse the caller's response for identity signals.

    Returns None if speech doesn't contain identity signals.
    Returns dict with {state, confidence, alt_name} if it does.

    Signal ladder:
      "No, this is Roy"                → DENIED,  0.9, alt="Roy"
      "This is Roy"                    → AMBIGUOUS, 0.6, alt="Roy"
      "I'm Roy"                        → AMBIGUOUS, 0.6, alt="Roy"
      "Roy calling on Sheila's phone"  → AMBIGUOUS, 0.5, alt="Roy"
      "Yeah" / "Yes"                   → CONFIRMED, 0.8
      "No" (alone)                     → DENIED,  0.7, no alt
    """
    lower = speech.lower().strip()
    expected_first = (expected_name or "").split()[0].lower() if expected_name else ""

    # Skip if caller just said the expected name back ("Yeah, it's Sheila")
    if expected_first and expected_first in lower and "no" not in lower:
        return {"state": VerificationState.CONFIRMED, "confidence": 0.8}

    # "No" prefix → denial
    has_negation = lower.startswith("no") or lower.startswith("nah") or lower.startswith("nope")

    # Name extraction: look for "this is {name}" / "I'm {name}" / "it's {name}" / "{name} here"
    alt_name = None
    for pattern_prefix in ["this is ", "i'm ", "it's ", "im ", "i am ", "my name is "]:
        if pattern_prefix in lower:
            after = lower.split(pattern_prefix, 1)[1]
            # Take first word as name (strip trailing phrases like "calling on...")
            candidate = after.split()[0].strip(".,!?") if after.split() else None
            if candidate and candidate != expected_first:
                alt_name = candidate.title()
                break

    # Also check "{name} here" / "{name} calling"
    if not alt_name:
        for suffix in [" here", " calling"]:
            if lower.endswith(suffix) or suffix + " " in lower:
                candidate = lower.split(suffix)[0].split()[-1].strip(".,!?")
                if candidate and candidate != expected_first:
                    alt_name = candidate.title()
                    break

    if has_negation and alt_name:
        return {"state": VerificationState.DENIED, "confidence": 0.9, "alt_name": alt_name}
    if has_negation:
        return {"state": VerificationState.DENIED, "confidence": 0.7}
    if alt_name:
        return {"state": VerificationState.AMBIGUOUS, "confidence": 0.6, "alt_name": alt_name}

    # No identity signals detected
    return None
