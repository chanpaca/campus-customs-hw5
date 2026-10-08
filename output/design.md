# Campus Customs Agent Desk — Design Notes

Visual and interaction design for the operations dashboard in `frontend/`
(React 19 + Vite + TypeScript).

The brief was an "authentic Yale Campus Customs operations desk." The word doing
the work there is **desk** — not a product landing page and not a chat UI. A desk
is where someone sits for an hour, watches work happen, and signs things. That
framing drove every decision below.

---

## 1. Color tokens

Yale Blue `#00356B` is the anchor. Everything else is derived from it or kept
deliberately quiet so the two things that matter — the cash number and the
approval card — are the only places the eye is pulled.

| Token | Value | Used for |
| --- | --- | --- |
| `--yale-blue` | `#00356B` | Masthead, section headings, selected-ticket rail, primary button |
| `--yale-blue-700` | `#002A56` | Masthead gradient end, button hover |
| `--yale-blue-300` | `#4A7AB0` | Hand-off arrows in the feed |
| `--yale-blue-100` | `#E6EDF5` | Fact chips, tool-name pills |
| `--yale-blue-050` | `#F3F7FB` | Selected and hovered ticket rows |
| `--bg` | `#F1F4F8` | Page background — cool grey-blue, not white |
| `--surface` | `#FFFFFF` | Cards and panels |
| `--border` | `#DBE3ED` | Crisp 1px card borders |
| `--ink` / `--ink-muted` / `--ink-faint` | `#16202C` / `#5A6B7E` / `#8B9AAB` | Three-step text hierarchy |

A single gold accent (`#D8B641`, the 3px rule under the masthead and the cash
drop flash) nods to the Yale palette without turning the page into school colors.

### Status colors

Each status is a hue plus a tinted background, never color alone — the label
always carries the meaning in words too.

| Status | Text | Background | Reading |
| --- | --- | --- | --- |
| Open | `#8A6D1F` amber | `#FDF6E4` | Untouched, waiting |
| Resolving | `#1B5FA8` blue | `#E7F0FA` | Agents are working — carries a pulsing dot |
| Needs Approval | `#A8560D` orange | `#FDF0E3` | **You** have to act |
| Resolved | `#15704A` green | `#E8F5EE` | Done |
| Blocked | `#A32E22` red | `#FBECEA` | Stopped, needs a decision |

### Agent colors

Five agents, five hues, so the feed is scannable without reading names:

| Agent | Color | Why |
| --- | --- | --- |
| Boss | Yale Blue `#00356B` | The house voice |
| Inventory | Teal `#0F6B5C` | Goods, shelves |
| Accounting | Umber `#8A4B12` | Money, ledgers |
| Facilities | Violet `#5B3A93` | Premises |
| Customer Service | Magenta `#9C2A5C` | The outward-facing voice |

All five were checked against their tinted backgrounds for contrast; the darkest
pair (Boss on `#E6EDF5`) clears WCAG AA comfortably at this weight and size.

---

## 2. Typography

- **Interface**: the system UI stack (`-apple-system, Segoe UI, Roboto…`). It
  renders instantly, needs no network request, and looks native on the machine
  it runs on — which is what a working tool should do.
- **Wordmark**: Georgia, a serif. The one typographic flourish, used only for
  "Campus Customs" in the masthead. It gives the shop a letterhead rather than a
  SaaS logotype.
- **Numbers**: a monospace stack for every figure — balances, amounts, ticket
  ids, tool names, counts. Money in a proportional font is hard to compare down a
  column; monospace makes `$2,560.00` and `$160.00` line up and read as data.

Scale is tight on purpose: 15px body, 11px uppercase section labels with heavy
letter-spacing, 29px for the cash readout, 32px for the approval amount. Only two
things are allowed to be large, and both are money.

---

## 3. Layout hierarchy

```
masthead     approver · desk date · CASH BALANCE        <- fixed context, always visible
─────────────────────────────────────────────────────
left rail    Ticket Board                               <- what work exists
(400px)      Actions (run / reset)                      <- what you can do
             Payments Ledger (appears once money moves)
─────────────────────────────────────────────────────
right column APPROVAL CARDS                             <- what needs you NOW
(fluid)      Ticket Summary (after a run)               <- what happened
             Live Agent Activity                        <- how it happened
```

Three deliberate choices:

1. **Cash lives in the masthead, not in a card.** It is the number every decision
   on this desk is judged against, and it must be visible while you are reading
   an approval card lower down the page.
2. **Approval cards sit above the feed, not inside it.** A pending payment is not
   an event in a log; it is a task assigned to the person at the desk. Burying it
   in a scrolling feed would make it possible to miss.
3. **The feed is last but tallest.** It explains and justifies everything above
   it, and it is where you look when you want to know *why* — not what to do.

The two-column grid collapses to one below 1000px, ordered so the ticket board
stays first.

---

## 4. Motion

Four animations, each with a job:

| Motion | Where | Purpose |
| --- | --- | --- |
| Pulsing dot | "Resolving" badge | Shows the team is working without a spinner |
| Rise + fade | Approval card mount | New card is noticed rather than appearing silently |
| Cash flash | Balance after approval | The gold flash + `− $2,400.00` delta makes the consequence of signing *felt* |
| Row fade-in | Feed events | Each new line is distinguishable from re-renders |

Everything else uses a 0.18–0.3s ease on hover and status transitions. Nothing
bounces, nothing slides in from off-screen. A desk tool should feel calm.

---

## 5. Human-in-the-loop UX decisions

This is the part the design is actually about. The system is built so agents
**cannot** move money; the interface has to make that legible rather than
incidental.

**The approver is named before anything can be signed.** The masthead carries an
"Approver on duty" field, persisted to `localStorage`. The approve button is
disabled while it is empty, with the reason stated under it. This is not
validation theatre — `approved_by` is written into the `payments` row and is the
permanent record of who decided. Making it the first thing in the header frames
the whole session: *a person is on duty here*.

**The approval card states the consequence before asking for the decision.**
Amount at 32px, payee, what it settles, how overdue it is, and `Balance after` —
all visible above the button. You should never have to compute what signing will
do.

**The card says who prepared it and that they could not approve it.** Every card
carries the line: *"Prepared by the accounting agent. Agents cannot approve their
own vouchers — this payment only reaches the ledger when you sign it."* The
constraint is explained at the moment it matters, not in documentation.

**Refusals are shown in the guardrail's own words.** When the backend rejects an
approval, the card renders the actual reason — *"'Boss' is an agent, not a human
approver"* — rather than a generic error. A guardrail that explains itself teaches
the operator how the system thinks.

**A settled voucher disappears and is replaced by a receipt.** Approving flips
the card to "Payment recorded" with the before/after balance, and the voucher
drops off the pending list once the ledger confirms it. Pending work and finished
work never look alike.

**The feed shows tool calls with their arguments and results.** Not just "Accounting
checked the balance" but `get_cash_balance {"account":"checking"}` → `balance:
3400.0`. The operator can audit the claim rather than trusting the summary — which
is the entire reason the facts flow through MCP.

**The feed starts at the live edge.** The audit trail is append-only and holds
every previous run. Replaying it on load would bury today's work under last
week's, so the dashboard fast-forwards the cursor on mount and shows only new
activity. (The full history remains in `output/audit_trail.json`.)

**Reset is a secondary button, next to the primary one but visually quiet.**
Wiping the desk back to $3,400.00 is useful during development and destructive in
spirit, so it is a ghost button rather than a second blue one.

---

## 6. Data flow

| Concern | Source | Cadence |
| --- | --- | --- |
| Tickets | `GET /api/tickets` | On load, after a run, after an approval |
| Cash | `GET /api/cash` | On load, after a run, after an approval |
| Events | `GET /api/events?since_seq=` | 1.2s while running, 4.8s when idle |
| Run status | `GET /api/runs/{run_id}` | Alongside the event poll while running |

Polling, not websockets: the run is a request/response operation with a known
end, and polling an append-only log is simple, restartable, and survives a page
reload. The cursor is the event's **`index`** — its position in the trail — not
`seq`, which restarts at 1 for each run and silently dropped new events until it
was fixed.

Vouchers are reconstructed from `draft_payment_voucher` tool results in the
stream rather than tracked in UI state, so the card shows the agent's own
figures, and a voucher already in the payments ledger is filtered out
automatically.

### Known latency

Every backend call that reads shop data spawns a fresh MCP subprocess, so
`/api/cash` takes roughly 3–5 seconds to answer. The balance therefore updates a
beat after you approve. The interface covers this with an `updating…` note in the
header rather than pretending it is instant; the real fix is a persistent MCP
session, which is a backend change rather than a design one.
