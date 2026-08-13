# Lead & Deal Lifecycle

Every lead flows from discovery on a licensed data source through qualification, a gated paid email lookup, and agentic email follow-up. OpenOutreach is browserless — there is no page navigation, no scraping, and no connect leg.

```
Discover (Lead Finder) → embed → Qualify (LLM) → QUALIFIED ─(GP gate)─▶ READY_TO_FIND_EMAIL
  licensed firmographics                            (Deal)              │ buy_address (submit)
                                                                        ▼
                                    free hub hit ─▶ READY_TO_EMAIL   FINDING_EMAIL ─(check_lookup poll)─▶ hit: READY_TO_EMAIL
                                                          │           provider job in flight        miss: NO_EMAIL_BETTERCONTACT
                                                          ▼
                                    email opener ─▶ EMAILED ──(only if they reply)──▶ COMPLETED / UNSUBSCRIBED
```

The authoritative state machine (with every transition and edge case) is in **[`../ARCHITECTURE.md`](../ARCHITECTURE.md) → Deal State Machine**. This page is the narrative summary.

---

## 1. Discovery (licensed, free)

**Where:** `core/pipeline/icp.py` → `core/pipeline/discover.py` → `discovery.py`

Discovery is a walk over **keyword sets**, not a single stored filter. One LLM pass (`icp.generate_seed`) turns the campaign's `product_docs` + `campaign_target` into opening single-word keywords and a headcount band; from there the vocabulary grows by *counting* the words that appear in profiles the LLM has already accepted. `select.py` scores each candidate keyword set from those labels and draws the next one to fire; `discover()` pages it from BetterContact **Lead Finder** — free, no emails — and persists each row as a `Lead` keyed on `profile_url` (stored, never fetched). A set that comes back empty is retired.

## 2. Embedding (at discovery time)

**Where:** `discovery.py:embed_row` → `core/db/leads.py:create_lead`

The lead's `profile_text` (headline, company description, title, seniority, industry, location) is built from the Lead Finder row and embedded (384-dim `BAAI/bge-small-en-v1.5`) onto `Lead.embedding`. No scrape, no re-fetch.

## 3. Qualification (LLM)

**Where:** `core/pipeline/qualify.py`, `core/ml/qualifier.py`

Embedded leads with no Deal are the pool. The GP selects which candidate to evaluate next — **exploit** (highest predicted probability) when negatives outnumber positives, else **explore** (highest BALD). Every decision is an LLM call over the stored `profile_text`. A campaign with no acceptances yet fits against **synthetic ideal profiles** written from its ICP (`icp.generate_anchors`, stored on `Campaign.anchor_profiles`), retired one per real acceptance.

- **Accepted** → `Lead` promoted to a `Deal` at `QUALIFIED`.
- **Rejected** → `FAILED` Deal with `wrong_fit` outcome (campaign-scoped; not `Lead.disqualified`).

## 4. Rank gate (QUALIFIED → READY_TO_FIND_EMAIL)

**Where:** `core/pipeline/ready_pool.py:promote_to_ready`

A GP confidence gate promotes `QUALIFIED → READY_TO_FIND_EMAIL` when `P(f>0.5) > min_gp_confidence` (0.9). This **rations the paid lookup** — only leads the model is confident about ever cost a credit.

## 5. Find email — two-leg async (READY_TO_FIND_EMAIL → READY_TO_EMAIL / NO_EMAIL_BETTERCONTACT)

**Where:** `emails/steps/lookup.py` — `buy_address` (submit) + `check_lookup` (poll)

`buy_address` tries the free cross-operator hub cache first (`contacts.resolve`) — a hit routes straight to `READY_TO_EMAIL`. Otherwise it fires a paid BetterContact job and parks the deal at `FINDING_EMAIL`, holding the `request_id` on the deal itself; `check_lookup` polls it:

- **hit** → `READY_TO_EMAIL` (address given back to the hub)
- **miss** (job done, no address) → `NO_EMAIL_BETTERCONTACT`, **blank outcome** (ML-skipped — an unfindable address is not a fit signal, so the labeler keeps the lead at label=1)
- **still running** → double `not_before` and ask again on the same `request_id`. There is no deadline and no attempt limit: an unterminated job is queued, not lost, and abandoning it would pay for a second one.
- **couldn't run** → back to `READY_TO_FIND_EMAIL` (no credit spent)

The submit leg only fires when there's mailbox send-headroom for the result today, so spend never outruns send capacity.

## 6. Opener (READY_TO_EMAIL → EMAILED)

**Where:** `emails/steps/send.py` → `core/agents/outreach.py`

The oldest `READY_TO_EMAIL` deal is picked when a mailbox is free to send. The outreach agent opens the conversation (its first-touch branch: a Mom Test question, not a pitch), it goes out over SMTP (BCC to the operator's own address on their own campaigns, never on freemium — `emails.sender.operator_bcc`), the send is recorded in the mail log and the deal points at the thread it opened, and the deal parks at `EMAILED`.

Three guards decide whether a box is free, all of them on **first emails only**:

| Guard | Where | What it bounds |
|-------|-------|----------------|
| Working hours | `core/sending_window.py` | Mon–Fri 08:00–20:00 in the operator's timezone |
| Spacing | `Mailbox.next_send_at` | ≥3 min + jitter between two openers from one box |
| Daily cap | `emails/warmth.py` → `Mailbox.daily_limit` | measured from the box's own Sent folder |

## 7. Replies (EMAILED → COMPLETED / UNSUBSCRIBED)

**Where:** `emails/steps/reply.py` → `core/agents/outreach.py` (same agent, now seeing a thread)

**Full documentation:** [`docs/outreach_agent.md`](outreach_agent.md)

Nothing is chased. The mail pass (`emails/mail_pass.py:run_mail_pass`) mirrors inbound mail into the log and threads it; a deal whose newest **turn** is inbound is what makes the agent run again — a bounce or an out-of-office is in the thread and is not a turn. It folds the new turns into the conversation summary and returns an `OutreachDecision`:

| Action | Effect |
|--------|--------|
| `send_message` | Threaded reply (`In-Reply-To` = latest, `References` = root). The deal stays `EMAILED`. |
| `mark_completed` | Close the Deal with the agent's `Outcome` |
| `suppress` | Honour a worded unsubscribe — account-wide, no reply sent |

Replies are exempt from all three send guards. An unanswered thread rests at `EMAILED` indefinitely and costs nothing, because nothing iterates it.

## 8. Terminal states

- **COMPLETED** — the agent closed the conversation (booked, declined, or went cold), with an `Outcome`.
- **NO_EMAIL_BETTERCONTACT** — no address could be resolved. Blank outcome, ML-skipped: the lead was a fit, only reachability failed.
- **UNSUBSCRIBED** — the recipient asked to stop, by reply or by the `+unsub` alias. Blank outcome, for the same reason.
- **FAILED** — an LLM qualification rejection (`wrong_fit`, campaign-scoped).

`Lead.disqualified=True` is a separate, permanent account-level exclusion (never given a new deal in any campaign), and is what actually enforces an unsubscribe.

## Freemium campaigns

Freemium campaigns draw candidates from a kit-ranked pool (`KitQualifier`) instead of the per-campaign GP, mint the Deal on the fly, and run the **email** funnel like any other campaign. They take their turn in the same rotation as your own campaigns, and never BCC you — that outreach is OpenOutreach's own conversation, not yours.
