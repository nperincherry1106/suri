# Suri — roadmap

This is the **forward-looking** plan. For history and the original v0 spec, see [`PLAN.md`](./PLAN.md). For what works today, see [`README.md`](./README.md).

**Principles (unchanged):** single user for now, Telegram as the main surface until a native app earns its place, SQLite + boring Python, no auto-send on high-stakes actions. Anything that **sends email on your behalf** or has **irreversible external effects** must pause for **explicit confirmation** (same family as today’s Outlook `[CONFIRM]` / Yes–No, extended to “send this Gmail” when we add that).

---

## Phase 1 — Plaid (money / recurring charges)

**Goal:** Suri can ground answers in **card and bank data** (recurring debits, merchants, amounts, dates), not just inbox text.

**Status (MVP in repo):** `plaid_start_link` → browser `/plaid/link/{session}` (Plaid Link) → `POST /plaid/exchange` → tokens in `plaid_items` table. Tools: `plaid_list_items`, `plaid_sync_transactions`, `plaid_recurring`. Webhook `POST /plaid/webhook` logs payloads (re-auth handling later). Set `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV`, and `SURI_PUBLIC_URL` (Plaid dashboard webhook: `<SURI_PUBLIC_URL>/plaid/webhook`).

- Add Plaid to the **server only** (client id/secret, env, Fly secrets). iOS is out of scope for this phase.
- Persist Plaid **items** and **access tokens** in SQLite (treat tokens as secret; log errors without leaking tokens).
- HTTP routes on the existing app (alongside the OAuth server): e.g. `link_token` creation, `public_token` exchange, **Plaid webhooks** (re-auth, transactions).
- Agent tools: small surface — list linked institutions, pull/sync transactions, return **structured** recurring or merchant rollups the model can cite.
- **User flow in Telegram:** you get a “connect your bank” link when needed; after that, “what’s hitting my card?” uses Plaid-backed data.
- **Exit:** sandbox works end-to-end; production Plaid is configured when you’re ready.

---

## Phase 2 — Gmail (full mailbox management, not read-only)

**Goal:** **Same class of capability as Outlook** — triage, search, labels/folders, trash/archive as appropriate, **draft replies**, and eventually **send**, with **you confirming before anything sends**. Read-only is **not** the end state; it’s the wrong mental model. The **safety** model is: **the agent can prepare and stage; it cannot complete a send without your explicit OK.**

**OAuth & scopes**

- Use Google’s OAuth with whatever **Gmail API** scopes are needed for: list/read messages, **modify** labels, trash, **create and update drafts**, and **send** (sending is behind the same confirm gate; scopes must allow the API call once you approve).
- Expect **sensitive / restricted** scope review if you go to production with a broad “manage Gmail” app; for a **personal, single-user** setup you still follow Google’s rules and use minimum scopes that match what tools actually do.

**Tools (shape mirrors Outlook, not byte-for-byte parity on day one)**

- Search + thread read (enough context for the model without dumping megabytes per turn).
- **Draft email** — create/update drafts in Gmail (you review in Gmail or in Telegram, depending on how we surface the body).
- **Send** — only via a **pending action** you confirm (e.g. token + short summary), analogous to not auto-sending Outlook mail today. Implementation detail: either Gmail `users.messages.send` after confirm, or “create draft + confirm sends” — but **no silent send.**
- Triage operations you already expect elsewhere: move to label, archive, delete (with confirmation where destructive, same spirit as Outlook batch deletes).
- Optional later: filters/rules if Gmail API and your patience align; not required to call Phase 2 “done.”

**Auth UX**

- Reuse the **“auth required → magic link in Telegram → replay”** pattern so you’re not stuck when the refresh token dies.

**Exit:** You can ask Suri to **manage** Gmail the way you expect for a second inbox (not just “scan for subscriptions”), and **outbound mail** is always **confirm-gated** before it leaves your account.

---

## Phase 3 — One brain: Plaid + Outlook + Gmail

**Goal:** One conversation can combine **card activity** (Plaid) with **receipts and human intent** (Outlook + Gmail) without a mess of duplicate lines on screen.

- Light **deduplication** or a small **“subscription candidate”** store (source = `plaid` | `outlook` | `gmail`, normalized merchant, last amount, last seen).
- Prompt / tool rules: when to hit Plaid vs. which inbox; optional `user_facts` key for “prefer Outlook vs Gmail for X.”
- Optional: one-line **proactive** context (e.g. morning brief) if it stays accurate — only after data quality is there.

**Exit:** “What do I pay for and what’s the evidence?” is answered using **more than one signal** on purpose, not by accident.

---

## Phase 4 — Native client (iOS) + thin API (when Telegram stops being enough)

**Goal:** A SwiftUI app is a **new front door**, not a second backend.

- **REST (or WebSocket) façade** on the same server: e.g. `POST` a turn, **stream** or poll replies, `GET` read models (subscriptions, Plaid link status).
- **Auth:** one long-lived key or device registration for a single user — keep it minimal until you need more.
- **Confirmations** reuse **`pending_actions`** (or the same token idea): the app approves a send or destructive op the same way Telegram does.
- **OAuth / Plaid Link:** `ASWebAuthenticationSession` (or open URL) to **your** already-public `SURI_PUBLIC_URL` flows; **no** third-party secrets in the app binary.

**Exit:** You can do the most important things from the phone app without reimplementing the agent.

---

## Explicit non-goals (unless you re-scope)

- Multi-tenant product, billing, and account management in these phases.
- Storing **streaming service** passwords to scrape “official” subscription UIs (Netflix, etc.) in place of email + Plaid.
- “General” subscription cancellation in the browser (the old v0 per-service Playwright work) as a **blocker** for Phases 1–3 — it can **follow** a working money + email story.

---

## Suggested build order

1. Plaid (sandbox → prod) + agent tools + webhooks  
2. Gmail integration with **full management** + **send only after confirm** + drafts  
3. Merge / dedupe layer + prompt polish  
4. Hardening (re-auth, token hygiene, Plaid `ITEM_LOGIN_REQUIRED` UX in chat)  
5. HTTP API + SwiftUI when you need it

---

*Last updated: 2026-04-26*
