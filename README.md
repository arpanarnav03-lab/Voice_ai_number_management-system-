# Bolna Number Manager

A small internal web app that tracks which phone number (caller ID) each Bolna voice agent
uses, counts **connected** calls per number, triggers calls from a dashboard, and moves an
agent to a fresh number once its current one reaches a call threshold. It never changes a
number while that agent is on a call.

It replaces the terminal MVP (`bolna_call.py`); the "connected call" logic lives on in
`backend/app/connected.py`.

## Safe by default

`BOLNA_MODE` defaults to **`fake`**: a simulated Bolna answers every request, so you can run
and demo the whole app without placing a real call. Only `BOLNA_MODE=real` reads
`BOLNA_API_KEY` and contacts `api.bolna.ai`. A badge in the header always shows which mode is on.

In fake mode a simulated call's outcome is decided by the **last digit of the recipient**:
`0-5` picked up (18 s), `6` voicemail, `7` no answer, `8` busy, `9` failed. Calls take about 9 s.
Fake mode also exposes a few obviously fake pool numbers (`+91800000000x`).

## Quick start

Requirements: Python 3.11+, Node 18+.

```bash
# backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fake mode, no login, SQLite
pytest                          # optional: run the tests

# frontend (builds into frontend/dist, which the backend then serves)
cd ../frontend
npm install
npm run build

# run
cd ../backend
python run.py                   # http://127.0.0.1:8000
```

Frontend development with hot reload: run `python run.py` in `backend/` and `npm run dev` in
`frontend/` (http://localhost:5173, proxies `/api` to port 8000).

### Seeding the pool

The app ships empty. Add numbers on the **Number Pool** tab, or from a CSV you supply:

```bash
cd backend
python seed.py my_numbers.csv   # one column: phone_number  (see numbers.example.csv)
```

Every number must be `+91` followed by 10 digits and must exist in Bolna's
`GET /phone-numbers/all` (the simulated list in fake mode).

### Going live

1. In `backend/.env` set `BOLNA_MODE=real` and `BOLNA_API_KEY=...`.
2. **Test on your own verified number first.** The trial account only allows outbound calls to
   verified numbers.
3. Consider setting `APP_PASSWORD`. Without it there is no login, and `run.py` refuses to
   listen on anything but `127.0.0.1` (anyone who can reach the app could place calls). With a
   password you can set `HOST=0.0.0.0`; put it behind HTTPS.

> **Compliance:** calling real people in India is subject to DND / TRAI rules and consent
> requirements. Make sure every recipient has agreed to be called and is not on a DND list.

## How it works

| Concept | Rule |
|---|---|
| Number states | `available`, `in_use` (held by exactly one agent), `limit_reached` (never assigned again until **Reset count**) |
| Connected call | Bolna status `completed`, duration >= `MIN_CONNECTED_SECONDS` (1), and not voicemail. Busy / no-answer / failed / etc. never count. A `completed` call with no duration is **Unclear**: not counted, flagged for review |
| Threshold | One global value (default 5), edited on the Number Pool tab. Never resets by itself |
| Call lock | A call is *active* from the moment it is placed until it is final. While an agent has an active call its number cannot change: manual change returns `409 AGENT_ON_CALL`; an auto-switch waits (`pending_switch`) until the last active call ends |
| Auto-switch | When a settled connected call hits the threshold, the number is released as `limit_reached` and the agent gets the first available number (pool order = date added) from the **same provider**. If none: the agent is `pool_empty`, its calls are blocked, and the next added/reset number fills waiting agents oldest-first |
| Bolna-linked numbers | Numbers Bolna reports with an `agent_id` (usually inbound) are warned about on add and excluded from auto-switching |
| Idempotency | `calls.execution_id` is unique and settlement checks `settled_at` inside the transaction, so an execution is counted once |

Invariants are enforced in the database: `agents.current_number_id` is `UNIQUE` (a number
serves one agent), partial unique indexes allow only one open assignment per number and per
agent, and on SQLite every transaction is `BEGIN IMMEDIATE` so "start a call" and "switch
number" cannot interleave. Calls are registered in the database **before** Bolna is contacted,
so the lock is taken in the same transaction as the check.

A background task polls `GET /executions/{id}` every `POLL_SECONDS` for each active call and
resumes automatically after a restart. A call with no final status after 30 minutes is marked
`unknown` and flagged, so an agent is never locked forever.

## Project layout

```
backend/
  app/connected.py     classify(): pure connected-call logic (tested)
  app/services.py      all rules: numbers, agents, lock, switching, settlement
  app/api.py           FastAPI routes (/api/...)
  app/poller.py        background settlement
  app/bolna_client.py  Bolna client (interface + real httpx client)
  app/fake_bolna.py    simulated Bolna (fake mode) + scripted fake (tests)
  app/auth.py          optional password login
  migrations/          Alembic (applied automatically at start-up)
  tests/               pytest
  run.py  seed.py
frontend/              React + Vite + TypeScript
```

## API errors

JSON `{ "code": "...", "message": "..." }`. Codes: `INVALID_PHONE`, `NUMBER_NOT_FOUND_ON_BOLNA`,
`NUMBER_IN_USE`, `NUMBER_LIMIT_REACHED`, `AGENT_ON_CALL`, `AGENT_HAS_NO_NUMBER`, `POOL_EMPTY`,
`AGENT_NOT_FOUND`, `BOLNA_ERROR`; plus `NUMBER_NOT_FOUND`, `NUMBER_ALREADY_IN_POOL`,
`AGENT_HAS_NUMBER`, `SWITCH_PENDING`, `INVALID_THRESHOLD`, `UNAUTHORIZED`, `VALIDATION_ERROR`.

## Privacy

The Bolna key lives only in `.env` and is read server-side. Transcripts and recordings are not
stored. Recipient numbers are stored (the data model requires it) but masked everywhere in
the API output and logs (`+91 98XXXXXX01`).

## Not built (by design)

Phase 5 (optional, ask first): syncing calls made outside the dashboard and "Sync numbers from
Bolna". Also out of scope for v1: campaigns/batches, buying numbers, roles, per-number thresholds.

## Sharing with a quick tunnel

To let your team use the app from their own computers, run a Cloudflare quick tunnel. It gives a
temporary public link such as `https://<random-words>.trycloudflare.com`.

1. Install the tunnel program once: `brew install cloudflared`.
2. Make sure the backend virtual environment and `frontend/node_modules` exist (see Quick start).
3. From the project root run:

   ```bash
   ./scripts/share.sh          # uses backend/.env
   ./scripts/share.sh --demo   # fake Bolna for this session only; .env is not touched
   ```

The script checks its requirements, starts the backend (or reuses one already running on port 8000),
builds the front end, serves the build on `127.0.0.1:4173`, opens the tunnel, keeps the Mac awake
and prints a banner and then the link. The backend itself stays on `127.0.0.1`; only the tunnel reaches it.

**Read the banner.** It always says whether the link needs a password or has **no login**, whether Bolna
is **LIVE (calls are real)** or **FAKE**, and whether Bolna deletion is **ON**. **Anyone who has the link
can use the whole app: with no password and live mode they can place real calls, and with deletion on they
can release (permanently delete) numbers from Bolna.** Only share it with people you trust.

- **Stop sharing:** press `Ctrl+C`. The link stops working at once and the next run gives a different one.
- The laptop must stay on and online while you share (the script keeps it awake with `caffeinate`).
- Logs are in `.share/` (ignored by git). The link is never written to a file.

### Record of changes

Because there is no login, every request that changes something (POST, PUT, PATCH, DELETE) is added to
`.share/actions.log`, one line each: time, method, path, response status and the caller's address
(`CF-Connecting-IP` when it came through the tunnel). Request bodies, query strings and keys are never
logged, and phone numbers in a path are masked to their last four digits.

```bash
tail -f .share/actions.log          # watch live
grep " 200 " .share/actions.log     # only the requests that succeeded
```

Set `ACTION_LOG_FILE` to use another file, or `ACTION_LOG_FILE=` (empty) to switch the record off.
