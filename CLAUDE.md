# PhoneBuddy — Claude's Context File

## Project Location
- **Repository**: `G:\repos\AI\PhoneBuddy`
- **GitHub**: https://github.com/bizcad/PhoneBuddy
- **Platform**: Windows 11, use `py` not `python`

## What This Is
AI phone receptionist built on Twilio + Claude + ElevenLabs. Screens inbound calls, classifies intent, routes intelligently, learns from every call.

**This is a product repo — not a planning workspace.** Planning, research, and session logs live in `G:\repos\AI\RoadTrip`.

## ⚠️ Critical Reminders

### Never commit secrets
- `.env` is gitignored — secrets live there and in `ProjectSecrets/`
- `ProjectSecrets/PAT.txt` holds the GitHub PAT for push automation
- `data/` is runtime-created and gitignored — never commit it

### Use `py` not `python`
```bash
# ✅ CORRECT
py -m uvicorn main:app --reload

# ❌ WRONG
python main:app
```

## File Structure
```
PhoneBuddy/
├── main.py               # Entire app — all endpoints and pipeline logic
├── requirements.txt
├── Dockerfile
├── railway.toml          # Railway deploy config (health check, restart policy)
├── .env.template         # Copy to .env and fill in keys
├── config/
│   └── user-profile.yaml # Owner profile, contacts, routing rules
├── ProjectSecrets/       # gitignored — local keys only
└── data/                 # gitignored — runtime logs and caller history
```

No `src/` nesting. Everything runs from repo root.

## SRCGEEE Call Pipeline (in `main.py`)
```
S  Sensation   — Twilio fires /call/inbound; extract caller metadata
R  Retrieve    — Load per-caller history + prior suspicion from data/history/
C  Classify    — Claude Haiku classifies intent with full context
G  Generate    — Select and render TwiML response
E1 Execute     — Return TwiML to Twilio (route the call)
E2 Evaluate    — Score confidence; log outcome
E3 Evolve      — Persist call record; emit telemetry
```

## Key Endpoints
| Endpoint | Purpose |
|---|---|
| `POST /call/inbound` | Twilio inbound webhook |
| `POST /call/gather` | Caller speech input |
| `POST /call/recording-complete` | Twilio recording callback |
| `GET /tts` | ElevenLabs TTS — called by Twilio `<Play>` |
| `GET /dashboard` | Live call dashboard (WebSocket) |
| `GET /health` | Health check — used by Railway and ACA |

## Environment Variables
See `.env.template`. Key ones:
- `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_PHONE_NUMBER`
- `ANTHROPIC_API_KEY` — Claude Haiku for classification
- `ELEVENLABS_API_KEY` — TTS voice (falls back to Polly if missing)
- `PUBLIC_URL` — must match the deployed URL so Twilio `<Play>` callbacks resolve

## Deployment

### Railway (primary)
`railway.toml` is already configured. Connect repo to Railway project, set env vars in Railway dashboard, push to deploy. Pro plan keeps container warm — no cold starts.

### Docker (local / any runner)
```bash
docker build -t phonebuddy .
docker run -p 8000:8000 --env-file .env phonebuddy
```

### Azure Container Apps
Deploy container to ACA with **min replicas = 1** (stay warm). Wire env vars via ACA secrets. Update Twilio webhook to ACA URL.

## Owner Profile
- **Owner**: Nick Stein (bizcad), preferred "Nick"
- **Twilio number**: +1 949 304 6155
- **Safe word**: "Nick here 1852" / "bizcad1852"
- **Cell**: +19493943466 (known contact — routes to admin mode)

## Local Dev
```bash
pip install -r requirements.txt
cp .env.template .env   # fill in keys
# start ngrok or Cloudflare Tunnel, set PUBLIC_URL in .env
py -m uvicorn main:app --reload --port 8000
# update Twilio webhook to your tunnel URL + /call/inbound
```
