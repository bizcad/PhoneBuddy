# PhoneBuddy Call Flow — Mermaid Diagram

Generated: 2026-04-03  
Source of truth: `G:\repos\AI\PhoneBuddy\main.py`  
Purpose: Input for PPA.api exhaustive test matrix generation

## Full Call Flow

```mermaid
flowchart TD
    START([Inbound Call]) --> INBOUND[/call/inbound]
    INBOUND -->|Known contact| FORWARD[Forward to cell]
    INBOUND -->|Unknown| SPAM["Play 'This is Nick'\nGather speech"]
    SPAM -->|Timeout x2| VM_ENTRY
    FORWARD --> DIAL_COMPLETE[/call/dial-complete]
    DIAL_COMPLETE -->|answered| END_CONNECTED([Call Connected])
    DIAL_COMPLETE -->|no-answer| RECLASSIFY[/call/classify\nRe-classify response]
    RECLASSIFY -->|wants voicemail| VM_ENTRY
    SPAM -->|Speech captured| CLASSIFY[/call/classify]
    CLASSIFY -->|DND flag: 3+ scam calls| BLOCK([Blocked: Hang up])
    CLASSIFY -->|Owner + safeword| ADMIN[/call/admin-query]
    CLASSIFY -->|Known contact| FORWARD
    CLASSIFY -->|"message" in transcript| VM_ENTRY
    CLASSIFY -->|Max attempts| VM_ENTRY
    CLASSIFY -->|Claude API| CLAUDE{Claude\nClassification}
    CLAUDE -->|medical / professional| HOLD[Hold + brief owner\nwaitUrl=/call/hold-music\n⚠️ route not implemented]
    CLAUDE -->|prospect| SURFACE[/call/surface-response\nFeature surface loop]
    CLAUDE -->|solicitation / scam / unknown| ENGAGE[/call/engage-response]
    SURFACE -->|YES| CLOSE_ENTRY[/call/close-response\nClose loop]
    SURFACE -->|ambiguous| SURFACE
    SURFACE -->|NO| NEXT_FEATURE{Features\nleft?}
    NEXT_FEATURE -->|yes| SURFACE
    NEXT_FEATURE -->|none left| WITHDRAWAL[Withdrawal pitch]
    WITHDRAWAL --> CLOSE_ENTRY
    CLOSE_ENTRY -->|"message"| VM_ENTRY
    CLOSE_ENTRY -->|YES| LEAD_CAPTURE["Lead capture\nname + number\n⚠️ posts to /call/engage-response\nnot a dedicated handler"]
    LEAD_CAPTURE --> VM_ENTRY
    CLOSE_ENTRY -->|NO attempt 1| PRIVACY_PROBE[Ask: worried about privacy?]
    PRIVACY_PROBE --> CLOSE_ENTRY
    CLOSE_ENTRY -->|NO attempt 2+| OBJECTION{Objection\ntype}
    OBJECTION -->|cost| REPLY_COST["It's free. Set it up?"]
    OBJECTION -->|time| REPLY_TIME[Five minutes. Set it up?]
    OBJECTION -->|trust| REPLY_TRUST[Tell me more]
    OBJECTION -->|other| REPLY_OTHER[Help me out...]
    REPLY_COST & REPLY_TIME & REPLY_TRUST & REPLY_OTHER --> CLOSE_ENTRY
    CLOSE_ENTRY -->|attempt >= 3| WITHDRAWAL2[Final withdrawal]
    WITHDRAWAL2 --> CLOSE_ENTRY
    CLOSE_ENTRY -->|timeout| VM_ENTRY
    ENGAGE -->|wants out / leave message| VM_ENTRY
    ENGAGE -->|YES + not yet pitched| PITCH[Play PB pitch]
    PITCH --> ENGAGE
    ENGAGE -->|YES + already pitched| SURFACE
    ENGAGE -->|cache hit| CACHE_REPLY[Instant cached reply]
    CACHE_REPLY --> ENGAGE
    ENGAGE -->|cache miss| FILLER[Play filler audio\npre-recorded thinking-time phrases]
    FILLER --> FOLLOWUP[/call/engage-followup\nClaude generates question]
    FOLLOWUP --> ENGAGE
    ENGAGE -->|timeout| VM_ENTRY
    VM_ENTRY[/call/voicemail] --> PB_PITCH_CHOICE[/call/voicemail-pb-choice\nWant to hear about PB?]
    PB_PITCH_CHOICE -->|YES| SHORT_PITCH[20-sec pitch]
    PB_PITCH_CHOICE -->|NO / timeout| RECORD
    SHORT_PITCH --> RECORD[/call/record-message\nRecord voicemail]
    RECORD --> MESSAGE_SAVED[/call/message-saved\nThank you. Goodbye.]
    MESSAGE_SAVED --> END_VM([Hang up])
    ADMIN --> END_ADMIN([Admin hang up])
```

## Known Gaps (as of 2026-04-03)

| Gap | Route | Impact |
|---|---|---|
| Whisper keypress handler | `/call/whisper-response` | Nick pressing a key after whisper may fail silently (Twilio contract lie) |
| Medical/professional hold music | `/call/hold-music` | Twilio Queue `waitUrl` points to unimplemented route |
| Dedicated lead-capture handler | `/call/lead-capture` | Hot leads currently post back to `/call/engage-response` and can drift into chat loop |

## Route Inventory

| Route | Method | Purpose |
|---|---|---|
| `/call/inbound` | POST | Entry point — known contact vs unknown |
| `/call/whisper` | POST | Whisper briefing before forwarding to Nick |
| `/call/whisper-response` | POST | ⚠️ NOT IMPLEMENTED — keypress after whisper |
| `/call/dial-complete` | POST | Dial outcome — answered vs fallback |
| `/call/classify` | POST | Claude classification + routing decision |
| `/call/admin-query` | POST | Owner admin mode |
| `/call/surface-response` | POST | Prospect feature surface loop |
| `/call/close-response` | POST | Close loop with objection handling |
| `/call/engage-response` | POST | General engagement loop (overloaded — also handles lead capture) |
| `/call/engage-followup` | POST | Claude generates next question on cache miss |
| `/call/voicemail` | POST | Entry to voicemail subtree |
| `/call/voicemail-pb-choice` | POST | Offer PB pitch before recording |
| `/call/record-message` | POST | Record voicemail |
| `/call/message-saved` | POST | Completion — thank caller |
| `/call/recording-complete` | POST | Twilio recording callback |
| `/call/hold-music` | POST | ⚠️ NOT IMPLEMENTED — Twilio Queue wait URL |

## Test Matrix References

- Full call scripts (21 scenarios): `G:\repos\AI\RoadTrip\workflows\014-Call-Buddy\CALL_FLOW_TEST_SCRIPTS.md`
- Adversarial failure matrix (18 cases): `G:\repos\AI\RoadTrip\workflows\014-Call-Buddy\PHONEBUDDY_ADVERSARIAL_FAILURE_MATRIX.md`
- FastAPI test harness plan: `G:\repos\AI\RoadTrip\memory\project_phonebuddy_test_harness.md`
