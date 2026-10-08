# Campus Customs — Multi-Agent Operations Desk

MGT 409 (AI Foundations for Managers), Homework 5.

A five-agent team that runs the operations desk at Campus Customs, a Yale apparel shop on
Chapel Street. Three tickets arrive on 2026-08-31 — a customer order blocked by an unpaid
vendor invoice, a rent notice, and a bulk discount request — and the team works them to a
decision while a human keeps control of the money.

**The rule the whole system is built around: agents prepare, humans approve.** The agents can
draft a payment voucher. Only a person can turn one into cash leaving the account.

---

## What's here

```
AI_prompts.md        running log of the prompts used to build this
requirements.txt     Python dependencies
.env.example         environment template — copy to .env
.gitignore           keeps the real .env out of git
.mcp.json            MCP client registration for the campus-customs-ops server
README.md            this file

data/
  campus_customs.db       pristine seed — never written to
  campus_customs_new.db   working copy the agents read and write

mcp_server/
  server.py          FastMCP server, 8 tools (6 read-only, 2 write-enabled)
  README.md          tool-by-tool reference

backend/
  main.py            FastAPI routes
  models.py          typed agent messages, delegations, tool results, ticket state
  agents.py          the five PydanticAI agents (gpt-6-luna, pinned)
  orchestrator.py    the execution loop and the human-approval path
  audit.py           append-only audit trail writer
  scripted_run.py    deterministic fallback runner (used when the LLM quota is exhausted)
  test_api.py        34-check API suite
  verify_cash.py     audits the Cash tab against the database
  prompts/           boss.md, inventory.md, accounting.md, facilities.md, customer_service.md

frontend/            React + Vite + TypeScript dashboard

output/
  harness.md             the operating manual: database, tools, agents, safety, routes
  design.md              dashboard design rationale
  desk_tickets.html      per-ticket plan (Expected), result (Actual), Cash, Reflection
  resolved_board.html    final board snapshot
  resolved_tickets.json  machine-readable resolution record
  mcp_smoke.json         MCP tool smoke test evidence
  audit_trail.json       every agent step, append-only
  github_url.txt         public repository URL
```

---

## Setup

### 1. Python dependencies

```bash
python -m pip install -r requirements.txt
```

### 2. Environment

```bash
cp .env.example .env
```

Then open `.env` and set your Portkey key:

```
PORTKEY_API_KEY=your_key_here
PORTKEY_MODEL=gpt-6-luna
```

`.env` is gitignored. `.env.example` is the only one that ships.

### 3. Frontend dependencies

```bash
cd frontend && npm install
```

---

## Running it

### Step 1 — Reset the working database

Every run starts from the pristine seed, so the desk opens at **$3,400.00** with all three
tickets `open`:

```bash
cp data/campus_customs.db data/campus_customs_new.db
```

On Windows PowerShell:

```powershell
Copy-Item data\campus_customs.db data\campus_customs_new.db -Force
```

`data/campus_customs.db` is never written to. Everything the agents do lands in
`campus_customs_new.db`, so this one command undoes a whole run. (The dashboard's
**Reset desk** button and `POST /api/reset` do exactly the same thing.)

### Step 2 — The MCP server

The server is launched automatically as a subprocess by both the backend and any MCP client
reading `.mcp.json`, so **you do not normally need to start it by hand**. To exercise it
directly:

```bash
python mcp_server/server.py
```

```bash
python mcp_server/server.py --http --port 8003
```

It exposes eight tools over stdio: `get_open_tickets`, `get_vendor_invoices`,
`get_lease_details`, `get_inventory_and_pricing`, `get_cash_balance`,
`draft_payment_voucher`, `record_approved_payment`, `update_ticket_status`. See
[mcp_server/README.md](mcp_server/README.md).

`.mcp.json` registers the server with paths **relative to the project root**, so an MCP
client should be started from this directory. If `python` on your PATH is not the interpreter
that has `fastmcp` installed, change the `command` field to that interpreter's full path —
which is exactly what the development machine does, since its system Python has a broken
FastMCP install.

### Step 3 — The FastAPI backend

```bash
python -m uvicorn main:app --reload --port 8000 --app-dir backend
```

or, equivalently, from the project root:

```bash
python -m uvicorn backend.main:app --reload --port 8000
```

Check it is up and can reach the MCP server:

```bash
curl http://127.0.0.1:8000/api/health
```

### Step 4 — The React frontend

```bash
cd frontend && npm run dev
```

The dashboard runs on <http://localhost:5173> and proxies `/api` to the backend on port 8000.
If your backend is on a different port, put `VITE_API_TARGET=http://127.0.0.1:8011` in
`frontend/.env.local`.

### Step 5 — Run the three tickets

In the dashboard:

1. **Ticket 101 — Bulldog tee.** Select it, click **Run Agent Team**. Inventory finds
   `CC-TEE-WHITE / S` at 0 on hand and traces the blocker to invoice 501 — $840.00 to Bulldog
   Print Co, 3 days overdue. Accounting drafts a voucher. An approval card appears: enter your
   name as **Approver on duty**, then click **Approve Payment**. The header balance drops
   **$3,400.00 → $2,560.00**.
2. **Ticket 102 — Rent due.** Facilities checks the email's claim against lease 1 — $2,400.00
   to Elm City Properties, due 2026-09-02 — and Accounting drafts the rent voucher. Approve it.
   Balance drops **$2,560.00 → $160.00**.
3. **Ticket 103 — Bulk hoodie quote.** Customer Service finds only 8 of the 20 size-M hoodies
   requested and Accounting caps the discount at 62.1% off the $58.00 list (the $22.00 cost
   floor). It produces a quote, not a sale — **the balance stays at $160.00**.

Ending state: all three tickets `resolved`, two payments totalling $3,240.00, ending balance
**$160.00**.

---

## Verifying a run

```bash
python backend/verify_cash.py
```

Parses the figures out of `output/desk_tickets.html` and checks each against the database —
balance, both payment rows, the ledger total, the arithmetic, and that no agent name appears
as an approver. 32 checks.

```bash
python backend/test_api.py --base http://127.0.0.1:8000
```

Exercises all eight API routes, including that approving a payment really debits
`cash_accounts` and that reset really restores the database file. 34 checks.

---

## Safety model

| Control | Where it lives |
| --- | --- |
| Agents cannot approve payments | `record_approved_payment` rejects agent names in `approved_by` |
| Agents cannot choose amounts | `draft_payment_voucher` reads the figure from the invoice or lease |
| No double payment, no overdraft | Refused in the same tool, before anything is written |
| Six of eight tools cannot write at all | SQLite `mode=ro` connection |
| Dates cannot drift | Everything is judged against `desk.date_today`, never the wall clock |
| Runaway delegation | `TokenBudget`: 10 hand-offs per ticket, enforced in the orchestrator |
| Nothing is lost | `output/audit_trail.json` is append-only with atomic writes |

Full detail in [output/harness.md](output/harness.md) §10.

---

## Known limitation — read before grading the agent runs

The Portkey key for `gpt-6-luna` is **over its usage limit** (HTTP 412, "Portkey API Key Usage
Limit Exceeded"), so no model could be reached and **no live agent run has taken place**.

The resolution recorded in `output/` was produced by `backend/scripted_run.py`, which follows
the plan written up front in `desk_tickets.html`. Every MCP tool call, database write, and
human approval in that record is real; the *routing decisions* were scripted rather than
model-chosen. Audit events from that run carry `mode: "scripted"` so they can never be mistaken
for model-driven ones, and the limitation is stated in every artifact it touches.

With a working key, `backend/orchestrator.py::run_ticket` rebuilds the same artifacts from
genuine agent output — that path is fully wired and was verified end to end with a stub model.
