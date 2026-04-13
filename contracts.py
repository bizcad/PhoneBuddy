"""
PB <-> PPA Communication Contracts

These Pydantic models define the typed interface between PhoneBuddy (I/O shell)
and the PPA (brain). PB sends Sensations, PPA returns Responses.

The two-pass R pattern:
  R1 (fast):  CRM lookup -> greeting instruction (< 50ms)
  R2 (async): full profile, history, predictions, SMBs (< 1500ms, runs during greeting)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


# =============================================================================
# ENUMS
# =============================================================================

class CallerKnowledge(str, Enum):
    """What PPA knows about the caller after R1."""
    KNOWN = "known"             # CRM match by phone number
    KNOWN_UNVERIFIED = "known_unverified"  # CRM match but might be shared line
    UNKNOWN = "unknown"         # no match
    SELF = "self"               # owner calling their own number


class VerificationState(str, Enum):
    """Identity confidence after soft verification exchange."""
    CONFIRMED = "confirmed"     # "yes" or matched voice
    DENIED = "denied"           # "no, this is Roy"
    AMBIGUOUS = "ambiguous"     # "this is Roy" (no y/n signal)
    UNVERIFIED = "unverified"   # haven't asked yet
    NOT_NEEDED = "not_needed"   # unknown caller, nothing to verify


class ConversationPhase(str, Enum):
    """Where PB is in the call flow."""
    GREETING = "greeting"       # first 3 seconds
    VERIFICATION = "verification"  # "is this Sheila?"
    LISTENING = "listening"     # caller is speaking
    RESPONDING = "responding"   # PB is speaking
    TRANSFERRING = "transferring"  # connecting to owner
    CLOSING = "closing"         # wrapping up
    VOICEMAIL = "voicemail"     # caller left message


class ResponseMode(str, Enum):
    """What PB should do with the PPA response."""
    SPEAK = "speak"             # TTS the utterance
    PLAY_AUDIO = "play_audio"   # play a pre-recorded file
    GATHER = "gather"           # speak then listen (Twilio Gather)
    TRANSFER = "transfer"       # Dial the owner
    VOICEMAIL = "voicemail"     # send to voicemail
    HOLD = "hold"               # play hold music/fillers


# =============================================================================
# PB -> PPA: SENSATION
# =============================================================================

class TwilioGeo(BaseModel):
    """Geo fields Twilio provides at ring time. Free data, always capture."""
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    country: Optional[str] = None
    caller_name: Optional[str] = None  # CNAM lookup (carrier-provided)


class PBSensation(BaseModel):
    """
    What PB sends to PPA on every event (call start, utterance, DTMF, hangup).

    This is the S phase output. PB classifies nothing -- it just packages
    what Twilio gave it and what it heard.
    """
    # Identity
    call_sid: str                           # Twilio CallSid
    caller_phone: str                       # E.164: +19493948986
    called_phone: str                       # E.164: the PB number

    # Event type
    event: str                              # "ring" | "utterance" | "dtmf" | "hangup" | "silence"

    # Content (depends on event type)
    utterance: Optional[str] = None         # STT text (for "utterance" events)
    dtmf: Optional[str] = None             # key pressed (for "dtmf" events)
    silence_ms: Optional[int] = None       # pause duration (for "silence" events)

    # Twilio metadata (captured at ring time, carried forward)
    geo: TwilioGeo = Field(default_factory=TwilioGeo)
    call_status: str = "ringing"            # Twilio CallStatus

    # Timing
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    call_elapsed_ms: int = 0               # ms since call answered

    # Transcript context (rolling window, PB maintains this)
    transcript_window: list[dict] = Field(default_factory=list)
    # Each entry: {"role": "caller"|"pb", "text": "...", "ts": "..."}


# =============================================================================
# PPA -> PB: RESPONSE
# =============================================================================

class PPAResponse(BaseModel):
    """
    What PPA sends back to PB after processing a Sensation.

    PB doesn't interpret this -- it just executes the mode.
    """
    # What to do
    mode: ResponseMode
    utterance: Optional[str] = None         # text to speak (SPEAK/GATHER modes)
    audio_file: Optional[str] = None        # filename to play (PLAY_AUDIO mode)
    transfer_to: Optional[str] = None       # phone number (TRANSFER mode)

    # Persona / tone (PB's TTS uses these)
    persona: str = "assistant"              # voice persona name
    emotional_tone: str = "friendly"        # "friendly" | "concerned" | "professional" | "warm"
    speech_rate: float = 1.0                # 0.8 = slow, 1.2 = fast

    # Conversation state (PPA tells PB where we are)
    phase: ConversationPhase = ConversationPhase.GREETING
    expects_response: bool = True           # should PB Gather after speaking?
    gather_timeout_sec: int = 3             # how long to wait for caller speech

    # Caller identity (PPA resolves this, PB just displays it)
    caller_name: Optional[str] = None
    caller_knowledge: CallerKnowledge = CallerKnowledge.UNKNOWN
    verification_state: VerificationState = VerificationState.UNVERIFIED

    # Filler management
    use_filler: bool = False                # play a filler while PPA thinks
    filler_hint: Optional[str] = None       # "acknowledgment" | "thinking" | "transition"

    # Timing
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# =============================================================================
# R1 RESULT (fast CRM lookup - internal to PPA, but useful for testing)
# =============================================================================

class R1Result(BaseModel):
    """
    R Phase 1: fast CRM lookup (< 50ms).
    Just enough to decide the greeting.
    """
    caller_phone: str
    caller_id: Optional[UUID] = None        # from caller table
    caller_name: Optional[str] = None
    caller_knowledge: CallerKnowledge = CallerKnowledge.UNKNOWN
    tags: list[str] = Field(default_factory=list)
    call_count: int = 0
    last_call_at: Optional[datetime] = None

    # Spoof detection
    geo_mismatch: bool = False              # area code vs Twilio geo disagree
    geo: TwilioGeo = Field(default_factory=TwilioGeo)


class R2Result(BaseModel):
    """
    R Phase 2: full profile retrieval (< 1500ms, runs async during greeting).
    Everything PPA needs to handle the conversation.
    """
    r1: R1Result                            # carry forward

    # Call history
    recent_calls: list[dict] = Field(default_factory=list)
    # Each: {call_sid, started_at, classification, completion_type, summary}

    # Predicted sensations (pre-staged DAG cache)
    predictions: list[dict] = Field(default_factory=list)
    # Each: {utterance_pattern, predicted_response, confidence, hit_count}

    # Nearest SMBs (semantic memory context)
    relevant_smbs: list[dict] = Field(default_factory=list)
    # Each: {id, title, summary, block_type, importance_score}

    # Profile data
    profile_json: dict = Field(default_factory=dict)

    # Ready flag
    ready: bool = False                     # True when all async queries completed


# =============================================================================
# VERIFICATION EXCHANGE (soft identity check)
# =============================================================================

class VerificationSignal(BaseModel):
    """
    Parsed result of the caller's response to "is this {name}?"

    Signal strength:
      "No, this is Roy"                -> DENIED,  high confidence, alt_name="Roy"
      "This is Roy"                    -> AMBIGUOUS, medium confidence, alt_name="Roy"
      "Roy calling on Sheila's phone"  -> AMBIGUOUS, medium confidence, alt_name="Roy"
      "Yeah" / "Yes"                   -> CONFIRMED, high confidence
      "No"                             -> DENIED,  high confidence (but no alt name)
    """
    state: VerificationState
    confidence: float = 0.5                 # 0.0-1.0
    alt_name: Optional[str] = None          # name they gave if not the expected person
    raw_utterance: str = ""                 # what the caller actually said
