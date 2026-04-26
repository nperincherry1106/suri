# v0 Build Plan: Personal AI Agent (SMS-first)

> **Note (post-build):** This is the *original* plan. The actual project pivoted
> away from SMS/Twilio (compliance issues — Twilio kept treating me as a business
> needing toll-free verification) to a Telegram bot, and from Gmail to Outlook
> (my main email is Outlook). It also expanded scope from "cancel one subscription
> end-to-end" to a broader life-admin assistant. See [`README.md`](./README.md) for
> what actually got built. This file is preserved for context on the original
> design intent and constraints.

**For:** Claude Code, building a working prototype for an audience of one (the founder).
**Goal:** Working SMS-based agent that can identify and cancel one of the founder's real subscriptions end-to-end, with one-tap approval.
**Time budget:** One weekend (Saturday + Sunday afternoon). If it takes longer, the scope was wrong — cut, don't extend.

---

## What we're building (and what we're not)

We are building a single-user agent (the founder texts a Twilio number, the agent texts back) that can do exactly one job at v0: scan the user's Gmail for active subscriptions, identify ones the user wants to cancel, and execute the cancellation via browser automation, with the user approving the final step over text.

We are **not** building: a multi-user system, a web UI, a native app, a fancy frontend, an auth system, account management, billing, or any of the other three v0 jobs (texts, ordering, eating). Those come later. Resist the urge to scaffold for them now — concrete v0.1 work is faster than abstract v0 architecture.

---

## Architecture (keep it boring)

**Components:**
1. **Twilio webhook receiver** — a small Flask or FastAPI server that accepts incoming SMS and sends outgoing SMS via Twilio's API.
2. **Agent core** — Python module that takes a user message + memory context, decides what to do, returns a response and/or triggers an action.
3. **Memory layer** — a SQLite database with three tables: `messages` (full conversation log), `user_facts` (key-value store for persistent context like name, preferences, known subscriptions), and `pending_actions` (actions awaiting user approval).
4. **Tools** — Python functions the agent can call:
   - `scan_gmail_for_subscriptions()` — uses Gmail API (OAuth, read-only) to find recurring charges over the last 90 days.
   - `cancel_subscription(service_name)` — uses Playwright to navigate to the cancellation flow, fills it out up to the final confirmation, returns a screenshot and a "ready to confirm" message.
   - `confirm_cancellation(action_id)` — clicks the final button on the previously paused Playwright session.
5. **LLM call** — Claude API (use Sonnet 4.5 for the agent reasoning loop). Tool use enabled.

**Stack:**
- Python 3.11+
- FastAPI for the webhook server (simpler than Flask for async)
- Twilio Python SDK
- Anthropic SDK
- google-api-python-client + google-auth for Gmail
- Playwright for browser automation
- SQLite (just `sqlite3` from stdlib, no ORM needed)
- ngrok for local development (Twilio needs a public URL to webhook to)

**Hosting:**
- Local dev only for v0. Run on the founder's MacBook with ngrok exposing the webhook. Don't deploy to a server until the agent works locally for two weeks.

---

## Data model

```
messages
  id INTEGER PK
  timestamp DATETIME
  direction TEXT  -- 'inbound' or 'outbound'
  body TEXT

user_facts
  key TEXT PK
  value TEXT
  updated_at DATETIME

pending_actions
  id TEXT PK  -- short token like 'CANCEL_PELOTON_X7K2'
  type TEXT   -- 'cancel_subscription'
  payload JSON
  status TEXT -- 'pending', 'approved', 'completed', 'rejected'
  created_at DATETIME

subscriptions_found
  id INTEGER PK
  service_name TEXT
  last_charge_date DATE
  amount REAL
  source_email_id TEXT
  cancellation_url TEXT
  status TEXT -- 'active', 'cancellation_in_progress', 'cancelled'
```

---

## The core agent loop

When an SMS arrives:

1. Log inbound message to `messages`.
2. Load recent message history (last ~20 turns).
3. Load all `user_facts`.
4. Load any `pending_actions` with status `pending`.
5. Build a system prompt that includes: the agent's persona (warm, concise, never sends more than 2 messages without a reason), the available tools, current context.
6. Send to Claude with tool use enabled.
7. If Claude calls a tool, execute it, append the result to the conversation, loop back to step 6.
8. When Claude returns a text response, send it via Twilio and log to `messages`.

**Two non-obvious agent design rules:**

- **Tool calls happen in the background; the user gets a short text first.** If the agent is about to scan Gmail (which takes 30+ seconds), it texts "on it, give me a sec" first, then texts the result. Do not make the user wait in silence.
- **High-stakes actions always pause for approval.** The `cancel_subscription` tool fills out the cancellation flow up to the final button, then stops, takes a screenshot, returns a `pending_action` ID. The agent texts the user something like "ready to cancel Peloton ($44/mo). reply YES CANCEL_PELOTON_X7K2 to confirm." The user replies, the agent calls `confirm_cancellation`.

---

## The persona prompt (starting point — iterate)

```
You are Namrita's personal assistant. You handle the small life-logistics
tasks that fall through the cracks because she's overwhelmed at work.

Tone:
- Warm but efficient. Like a competent friend, not a butler.
- Lowercase is fine. Casual contractions are fine.
- Never more than 2 SMS in a row without a reason. Brevity is respect.
- No emoji unless she uses them first.
- Never apologize unless you actually messed up.

Behavior:
- Default to action, not asking. If you can take a reasonable next step,
  take it and report back.
- For high-stakes actions (canceling things, sending messages, spending
  money), always pause for explicit approval before executing.
- If you're going to take >10 seconds to do something, send a short
  acknowledgment first ("on it") before working.
- Remember things. If she's told you something before, don't ask again.

Boundaries:
- Never pretend to have done something you haven't.
- If a tool fails, say so plainly. Don't make up results.
- If you're not sure what she wants, ask one short question. Not three.
```

---

## v0 acceptance test

The build is done when this scenario works end-to-end without intervention:

1. Founder texts the Twilio number: "find subscriptions I'm not using"
2. Agent replies: "on it" within 5 seconds
3. Agent scans Gmail, returns within 60 seconds: a list of 3-5 recurring charges with last-used estimates
4. Founder texts: "cancel [one of them]"
5. Agent replies: "ready to cancel [service] ($X). reply YES [TOKEN] to confirm"
6. Founder replies: "YES [TOKEN]"
7. Agent executes the cancellation via Playwright, texts back "done. confirmation: [details]"
8. The subscription is *actually canceled*. Verifiable by checking email for the cancellation confirmation.

If steps 1-8 work for one real subscription, v0 is shipped. Don't gold-plate.

---

## Build order (Saturday)

**Hour 1: Twilio echo bot.** FastAPI server, Twilio webhook, agent texts back what it received. Verify SMS works in both directions.

**Hour 2: Claude integration.** Replace the echo with a Claude call. Agent now has personality and conversation memory in SQLite. Verify it remembers things across messages.

**Hour 3: Gmail tool.** OAuth flow (one-time), implement `scan_gmail_for_subscriptions()`. Test by running it directly first. Then wire it as a Claude tool.

**Hour 4: Playwright tool — but stub the cancellation.** Pick *one* specific subscription the founder wants to cancel (e.g., Peloton or NYT). Hand-write the Playwright script for *just that service*. Don't generalize yet — generalization is v0.5.

**Hour 5: Pending actions + approval flow.** The `pending_actions` table, the token system, the YES TOKEN parsing.

**Hour 6: End-to-end test.** Run the acceptance test above with the real subscription. Fix what breaks.

## Build order (Sunday afternoon — only if Saturday worked)

- Add a second hand-written cancellation flow (different service).
- Tighten the persona — read back actual transcripts, edit the prompt where the agent's voice felt off.
- Add basic error handling: what happens if Gmail OAuth expires, if Playwright fails mid-flow, if Twilio rate limits.
- *Stop.* Anything beyond this is v0.1.

---

## What you'll learn from v0

- Whether the texting interaction *actually feels good* or whether it feels janky. This is the most important thing. If it feels janky, no amount of feature work fixes it.
- Where the agent's voice/persona breaks down. The prompt above is a guess; the real prompt comes from reading 200 messages of actual transcripts.
- Whether scanning Gmail for subscriptions surfaces useful or noisy results. The detection logic might need to be much smarter than v0's "look for recurring charges."
- How fragile Playwright cancellation flows are. Some sites will fight back (retention offers, multi-step confirmations, captchas). This is a real moat if you solve it well.
- What you actually want to add next. Don't decide v0.1 scope until you've used v0 for two weeks.

---

## What NOT to build into v0

- Multi-user support. Single hardcoded user. No auth.
- Voice messages. Text only.
- Image attachments in SMS (MMS). Skip.
- Calendar integration. Different job.
- Any UI other than SMS. No web dashboard, no app, no nothing.
- Generalized cancellation. Hand-write per-service flows. The framework comes after you've written 5-10 by hand and see the pattern.
- Fancy memory architecture. SQLite + key-value is fine for v0. Vectors and embeddings are v0.5.
- Tests. (Controversial, but for a one-weekend prototype: no. Test by using it. Add tests when you have a second user.)

---

## When to throw this plan away

If after Hour 2 the texting interaction feels lifeless or wrong, stop and re-think the persona before building further. The whole product is the texting feel. Everything else is plumbing.

If you discover the Gmail-scanning approach surfaces mostly noise (random one-time charges, false positives), the v0 job might need to be different — maybe "the user tells the agent which subscription to cancel" rather than "the agent finds them." That's a v0 pivot, and it's fine. The cancellation execution is still the core capability.

---

## Founder note to self

This is a prototype for an audience of one. It does not need to be good code. It does not need to scale. It needs to *exist* and let you feel what it's like to text an agent that handles your life. Build it dirty, use it for two weeks, then decide what's real.
