# Outreach Agent

One agent runs the whole email conversation, from the cold open to the last reply. It is doing **Mom Test research, not selling**: the goal of a thread is a candid answer about how the lead works today, not a booked meeting. It is a self-rescheduling loop — every decision that isn't `mark_completed` re-arms the deal's clock, so the daemon keeps checking back until the conversation ends.

The cold open and the in-thread replies are the same voice doing the same job, so they render from **one** prompt (`core/templates/prompts/outreach_agent.j2`) which branches on the only thing that differs: whether a thread exists yet (`deal.email_message_id`).

## Flow

```
READY_TO_EMAIL deal                       EMAILED deal, due by next_follow_up_at
        │                                         │
handle_email()                            handle_follow_up()
  ← emails/tasks/send.py                    ← emails/tasks/follow_up.py
        │                                         │
        │                                         ├─ read replies  ← emails/inbox.py:sync_inbox (IMAP: match the
        │                                         │                  thread root in References/In-Reply-To, upsert
        │                                         │                  new replies as ChatMessage, fold into chat_summary)
        └─────── run_outreach_agent() ────────────┘
                 ← core/agents/outreach.py
```

## Decision

`run_outreach_agent` builds context (campaign docs + booking link, the lead's `profile_summary`, and — in thread only — the `chat_summary` plus a recency window of verbatim messages) and makes **one** structured LLM call returning an `OutreachDecision`:

| Action | Effect |
|--------|--------|
| `send_message` | **First touch:** the decision also carries a `subject`; `handle_email` sends it, writes the thread root, and moves the deal to `EMAILED`. **In thread:** threaded SMTP reply via `emails/sender.py` (`In-Reply-To` = latest message, `References` = thread root); records the outgoing `ChatMessage`; re-arms `next_follow_up_at` from the agent's own `follow_up_hours`. |
| `wait` | Push `next_follow_up_at` out, no send. |
| `mark_completed` | Close the Deal `COMPLETED` with the agent's `Outcome`. |

A first touch is constrained to `send_message` **with** a `subject` (`_validate_opener`) — there is nothing to wait for or complete before the thread exists. The LLM owns pacing end-to-end via `follow_up_hours` (there is no hardcoded default). Those are **business** hours: `core/business_time.add_business_hours` stamps the countdown so weekend hours don't tick and no follow-up comes due on a Saturday or Sunday, and the prompt is told the thread's age in **working** days (`business_days_between`) rather than calendar days. Sends are bounded by the per-mailbox daily cap.

## Summaries

All summary LLM calls go through `core/db/summaries.py` (mem0-style):

- `materialize_profile_summary_if_missing(deal, session)` builds `profile_summary` before the opener, from the lead's **stored** `profile_text` — no re-scrape (there is no profile to fetch).
- `update_chat_summary(deal, new_messages, seller_name=…)` folds newly-read replies into `chat_summary` via `reconcile_facts` (mem0 ADD/UPDATE/DELETE/NONE). The mem0 update prompt is vendored under `core/vendor/mem0/`.

The `chat_summary` fact list is where a thread's research value currently accumulates — free text, not structured fields.

## Prompt

One template, `core/templates/prompts/outreach_agent.j2`, keeping the structure of the LinkedIn-era prompt it descends from: `Strategy` / `Actions` / `Timing` / `Capabilities` / `Rules`, with the context blocks above them.

`## Strategy` is still the **two modes** it always was, re-weighted:

- **Discovery** — the default, and where the agent stays. Understand their world without mentioning the product. Carries the standing list of what to steer toward (company/team shape, current workflow, current tooling and its cost, the last thing that broke, the trigger, who else decides), because what we learn here is the point of the thread.
- **Pitching** — entered only on an explicit pull (they ask what you do, ask how you could help, or ask for a call). A problem the product solves is explicitly *not* a cue to pitch — it's the cue to dig deeper. When it does happen: one or two plain sentences, then back to discovery.

That re-weighting is the whole behavioural change. The earlier version entered Pitching as soon as the conversation "revealed a concrete problem our product solves", and worked toward "a concrete next step (booking link, trial, demo)".

See [Template Variables](./template-variables.md).
