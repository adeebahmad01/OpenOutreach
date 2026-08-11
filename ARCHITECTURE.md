# Architecture

Detailed module documentation for OpenOutreach. See `CLAUDE.md` for rules and quick reference.

OpenOutreach is a browserless, **email-first** AI sales agent: it learns a campaign's ICP
and runs the whole funnel — **define ICP → discover → qualify → rank → find email → agentic
email** — off licensed data, with no social-network account and no scraping.

## Project Layout

All source lives in the single `openoutreach/` package; Django apps are nested inside it
(dotted `AppConfig.name`, short labels). One engine, one outreach channel:

```
manage.py
tests/
openoutreach/
  settings.py        # Django settings (SQLite at data/db.sqlite3)
  urls.py
  discovery.py       # Lead Finder client (ICP search + row embedding) — the top of the funnel
  core/              # engine app (label: core) — the cycle, operator lookup,
                     #   Campaign/SiteConfig models, llm.py, conf.py, onboarding,
                     #   ML (qualifier/embeddings/kit), discovery+qualify pipeline,
                     #   the two agents, db/ helpers, geo, management commands,
                     #   vendored mem0
  emails/            # channel app (label: emails) — enrichment (BetterContact), Mailbox +
                     #   import + SMTP/IMAP, sender, the mail pass, steps/
  crm/               # app (label: crm) — Lead, Deal
  chat/              # app (label: chat) — ChatMessage (the per-Deal conversation)
  legacy/            # model-less app (label: legacy) — migration-history anchor only
  contacts/          # central contacts-store client (service.py only — no models, not an app)
```

Layering: `core` owns orchestration, the ML/discovery/qualify pipeline, and the
channel-agnostic models; the `emails` app owns the enrichment + send/read mechanics and the
steps. `core` imports channel code only at wiring points (the cycle's hierarchy).

**No social network.** The browser, the network's private API, connect/check_pending, and the
browser-CLI dependency were removed in the email-first pivot. The `legacy` app is intentionally
model-less — it exists only to anchor migration history that `core`/`crm` depend on so
existing installs stay on a forward-only, backward-compatible migration graph (the retired
profile/keyword/action-log models were deleted in `legacy/0012`).

## Entry Flow

`manage.py` — stock Django management entrypoint. Bare `python manage.py` (no subcommand, or a
leading flag) defaults to `rundaemon`. A global `--db PATH` (or `--db=PATH`) is stripped from argv
before Django parses it and exported as `OPENOUTREACH_DB`, which `settings.py` reads for the SQLite
file (default `data/db.sqlite3`); the parent directory is created if missing.

### `rundaemon` management command (`management/commands/rundaemon.py`)

Startup sequence:
1. **Configure logging** — level from `--verbosity`, banner, noisy third-party loggers silenced (`core/logging.py`).
2. **Ensure DB** — `migrate --no-input` (the custom migrate; see below) + `setup_crm` (idempotent).
3. **Onboard** — if `missing_keys()` is non-empty: interactive wizard on a TTY, else print what's missing and exit (no TTY, no silent partial start).
4. **Validate the operator** — `llm_api_key` set, an active operator `User` exists, at least one campaign. All three exit loudly rather than starting a daemon that cannot do anything.
5. **Run** — `run_daemon()` (`core/cycle.py`). No session object is built: the campaign rides on the deal and the operator is looked up (`core/operator.py`).

Docker's `start` script `exec`s `python manage.py rundaemon` (no Xvfb/VNC — there is no browser).

### Other management commands

- `migrate` — **overridden** (`management/commands/migrate.py` + `core/migration_compat.py`): before Django's migration-consistency check runs, it relabels any `linkedin` rows in `django_migrations` to `legacy`, so a pre-pivot DB upgrades with a plain `migrate` (no manual SQL, no `--fake`). Idempotent no-op on fresh installs.
- `setup_crm` — idempotent CRM bootstrap (default Site).
- `reset_data` — wipe pipeline data for a fresh run.

## Onboarding (`core/onboarding.py`)

Email-first, built as an **ordered list of idempotent steps** (`STEPS`). Each `Step` is a
`(key, is_done, run)` triple: `is_done()` reads the DB (never prompts), `run()` collects what's
missing and **persists it the moment it succeeds**. `onboard_interactive()` runs only the steps
whose `is_done()` is false, in order — so a partial onboarding resumes exactly where it stopped and
a satisfied step is never revisited. There is no end-of-wizard `apply()` that could half-fail; each
step is its own commit point.

```
campaign        product description + target + booking link → Campaign row
llm             LLM creds, live-verified via verify_llm_credentials (retries in place on failure)
mailbox         field-by-field SMTP box → auth-check → Mailbox row; retries with values retained
signature       sign-off per never-asked (NULL) box; "" = declined and sticks
bettercontact   API key (mandatory — the SAME key powers Lead Finder discovery AND enrichment)
account         your email (contacts key + newsletter target, optional BCC) → country → newsletter (opt-in) → legal (required gate) → operator User + subscribe
```

- Cancellation is a **single exception**: prompts return `None` on Ctrl+C, `_required()` turns that into `OnboardingCancelled` at one boundary, and the mailbox step catches it (cancel with a box already connected just stops adding more; cancel with none aborts).
- A failed step re-asks **its own** fields (mailbox retries retain what you typed; LLM retries re-verify) — it never rewinds to an earlier step or restarts the wizard. This is what fixed the "SMTP onboarding keeps looping back" bug, together with `emails/smtp.verify_auth` now selecting the transport by port (implicit SSL on 465, STARTTLS on 587) instead of hard-coding `starttls()`.
- The operator's email **is asked** (the `account` step) and stored as `User.email` — it is the **human's own inbox**, deliberately distinct from the mailbox `from_address` (the sending robot). The newsletter subscribes it and the contacts give-back keys the operator; the daemon BCCs a copy of every send here too on the operator's **own** campaigns, and **never** on a freemium one — the whole rule is `emails.sender.operator_bcc(user, campaign)`, which both `send.py` and `follow_up.py` call instead of deciding for themselves. Freemium outreach is OpenOutreach's own conversation, not the operator's, so there is no copy to give (and a blank `User.email` yields `None`, not an empty `Bcc` header). **The send log follows the same split**: `sender._sent_block` logs the full message text — as assembled, so signature, opt-out and attribution included — on the operator's own campaigns, and freemium sends stay metadata-only. It discloses nothing new, since the BCC already puts every one of those messages in the operator's inbox. The rule is *not* keyed off `bcc`, which is `None` for two different reasons; `send_email` takes `campaign` explicitly and defaults to metadata-only, so a call site that forgets cannot leak a body. The `From:` header stays the mailbox `from_address`. `account`'s `is_done()` requires an active staff `User` with a **non-blank** email, so a blank-email account can't short-circuit the address prompt.
- The **signature** is its own step, not a field of the `mailbox` step, and that separation is load-bearing. `mailbox`'s `is_done()` is `has_mailbox()`, so once any box exists the step never runs again — a signature asked *inside* it could only ever reach operators who onboarded after it shipped, and every pre-existing install would send unsigned mail forever with nothing to notice it by (that was the `0002` bug, fixed by `0003`). The step keys on **NULL, not emptiness**: NULL means never asked, `""` means declined and must stick, or a declining operator is re-prompted on every startup. It is appended to every send from that box by `sender._sign` — openers and follow-ups alike, since both go through `send_email` — and `sender._attribute` then adds the always-on `Sent with OpenOutreach` line after it (two blank lines down, and deliberately unlinked — a bare product name reads as a footer where a URL reads as an ad), so the operator's own sign-off never carries the attribution. It lives on `Mailbox` rather than `SiteConfig` because it is part of the sending identity, and it stays editable in the Django Admin. The outreach prompt still forbids the agent from signing its own drafts (`prompts/outreach_agent.j2`), so there is exactly one sign-off.
- `missing_keys()` returns the keys of unsatisfied steps (`campaign`/`llm`/`mailbox`/`signature`/`bettercontact`/`account`), so the daemon knows onboarding is incomplete until every gate passes.
- The newsletter opt-in **default** is jurisdiction-aware (off in GDPR/opt-in countries via `core/geo.is_gdpr_protected`), but an explicit yes always subscribes (lawful consent anywhere). Nothing is persisted in the `account` step until the Legal Notice is accepted.
- The interactive wizard is vendored in `onboarding_wizard.py`: thin `text`/`integer`/`confirm`/`multiline` functions over questionary/prompt_toolkit, each owning its own validation loop and returning a value or `None` (cancel). No external `openoutreach` package dependency.

## Deal State Machine

`crm/models/deal.py:DealState` (OpenOutreach-owned `TextChoices`) is the whole funnel — a lead
is discovered and qualified **without** an email in hand (Lead Finder returns firmographics, not
addresses), so the funnel first *finds* the email and then *talks*:

```
QUALIFIED ─(GP rank gate)─▶ READY_TO_FIND_EMAIL ──(buy_address)──▶ FINDING_EMAIL ──(check_lookup)──▶ hit:  READY_TO_EMAIL
 discovered + qualified      ranked, awaiting the      provider job in flight;      miss: NO_EMAIL_BETTERCONTACT
 (no email yet)              paid lookup               request_id on the deal               │
                            (free hub hit → READY_TO_EMAIL directly, no job)                ▼
                          READY_TO_EMAIL ──(first email)──▶ EMAILED ──────────────────▶ COMPLETED / FAILED
                                                             │                         └▶ UNSUBSCRIBED
                                                             └ no reply → nothing further is ever sent
                                                               they reply → agent: send / complete / suppress
```

- **`READY_TO_FIND_EMAIL`** — passed the **GP confidence gate** (`ready_pool.promote_to_ready` above `min_gp_confidence`); queued for the *paid* lookup (one credit per verified hit). The gate rations spend to leads the model is confident about; the submit leg additionally fires only when there's mailbox send-headroom for the result today.
- **`FINDING_EMAIL`** — a provider job is in flight; the deal is excluded from the candidate pool (so the next cycle can't re-select it and double-charge) while `check_lookup` polls to termination. The job handle (`lookup_request_id`) and the poll backoff (`lookup_attempt` + `not_before`) live **on the deal**, so an in-flight lookup survives a restart and its wait gates that one row and nothing else.
- **`READY_TO_EMAIL`** — an address exists; queued for the first email. A cheap, **ungated** FIFO send-queue paced by the per-box send interval and the per-box measured capacity (no ranking step).
- **`EMAILED`** — the first email has been sent, and **nobody is ever chased**. The deal becomes actionable again only when the recipient replies: the mail pass writes inbound `ChatMessage` rows, and a deal whose newest inbound message is newer than its newest outgoing one is what the agent serves (`cycle.unanswered_replies`). No reply means no further email, ever — so `EMAILED` is where most deals come to rest, at no cost, since nothing iterates them.
- **`UNSUBSCRIBED`** — the recipient asked to be left alone, by a mail client's unsubscribe button (found box-wide by the mail pass's `+unsub` alias check) or in words the outreach agent read. The **sibling of `NO_EMAIL_BETTERCONTACT` on the far side of the send**: a fit positive whose *reachability* ended, not a verdict on the offer — so `outcome` stays blank exactly as it does on an enrichment miss and the ML labeler keeps the lead at label=1. The *enforcement* is not this state (which would bind one campaign) but `Lead.disqualified`; see **Opt-out and suppression** below.

**The paid lookup is a two-leg async handshake** (mirroring the retired connect→check_pending). `find_email` (submit) resolves free-hub-first (hit → `READY_TO_EMAIL` with no job/credit), else fires a provider job and parks at `FINDING_EMAIL`; a couldn't-submit (no key / API down) stays `READY_TO_FIND_EMAIL`. `collect_email` (poll) is then **tri-state**: hit → `READY_TO_EMAIL` (address given back to the hub); **miss** (job terminated, no address) → `NO_EMAIL_BETTERCONTACT` — its own terminal, with **no reason and blank outcome**, critically not `FAILED+wrong_fit`, which the ML labeler reads as a negative: a lead we simply couldn't reach was still an LLM fit positive and stays label=1; **still running** → chains the next poll with doubled backoff on the same `request_id`, with no deadline and no attempt limit (see `handle_collect_email` below for why the deadline was removed).

`crm/models/deal.py:Outcome` (TextChoices): converted, not_interested, wrong_fit, no_budget,
has_solution, bad_timing, unresponsive, unknown — on `Deal.outcome`. `Lead.disqualified=True` =
permanent account-level exclusion (never given a new deal). LLM qualification rejections =
`FAILED` deals with `wrong_fit` outcome (campaign-scoped). Pre-Deal Lead states are implicit:
url-only (a `Lead` row with a null `embedding`) vs embedded (has an `embedding` + `profile_text`,
awaiting qualification).

*(The retired connect leg — `READY_TO_CONNECT`/`PENDING`/`CONNECTED`, the connect/check_pending
retry+backoff columns — was removed with the channel. Existing deals stranded at those states are
remapped to `QUALIFIED` on upgrade so they re-enter the email funnel.)*

## The Cycle

**A queue is a status, not a table.** Work is found by asking the deals what they need
(`Deal.objects.filter(state=…)`), so a deal is available because of its own row. Nothing is created
in advance, so nothing can drift, be lost, or need reconciling — and no row's timestamp can gate
anything but itself.

The loop is `core/cycle.py`:

```python
while True:
    read_mail_if_due()           # one IMAP walk per box: replies + opt-outs
    refresh_capacities_if_due()  # daily warm-capacity measurement
    run_one_action(next(rotation))
    time.sleep(CYCLE_SECONDS)    # 5s, fixed — no data decides when we next wake
```

### What replaced, and why

`core/scheduler.py` wrote `Task` rows that were **permission tokens** stamped with a time — "at
14:32 someone may send one email for campaign A" — without saying to whom; the loop took the
earliest-due token and the handler then went and found its own target. When no token was due the
daemon slept **until the earliest token's timestamp**. On 2026-08-05 that put a live install to
sleep for 34 hours: two BetterContact polls had never terminated, their uncapped backoff had
reached 45h30m, they were the only rows in the table, and 55 ready deals with 70 sends of headroom
had no token and therefore were not work. The shape was inherited from the Playwright era, when a
token queue was how access to one browser got serialised; the browser went, the queue outlived it.

Deleted with it: `Task`/`TaskQuerySet`, `core/scheduler.py` (558 lines), `core/quota.py` (172) and
`Campaign.action_fraction`, `core/session.py`, `add_business_hours`, and `emails/tasks/` (598).

### The hierarchy

`run_one_action(campaign)` walks one ordered list and stops at the first thing it can do, so
priority is exactly the order these are written in:

| # | State | Step | Condition |
|---|---|---|---|
| 1 | `FINDING_EMAIL` | `check_lookup` | `not_before` elapsed |
| 2 | `EMAILED` + unanswered reply | `answer_reply` | a lead wrote back |
| 3 | `READY_TO_EMAIL` | `send_first_email` | a mailbox is free |
| 4 | `QUALIFIED` | `promote_to_ready` | — |
| 5 | `READY_TO_FIND_EMAIL` | `buy_address` | room to send today |
| 6 | *(the campaign itself)* | `top_up` | room to send today |

A state that is not listed is terminal, and terminal costs nothing: `NO_EMAIL_BETTERCONTACT`,
`UNSUBSCRIBED`, `COMPLETED`, `FAILED` — and `EMAILED` with no unanswered reply.

**Rows 5 and 6 share one condition, and it is the only spend control there is**
(`cycle.room_to_send_today`): never resolve an address, and never spend an LLM call qualifying, for
someone there is no room to email today. Idle is the *normal* state — sends are minutes apart and a
cycle is seconds — so "nothing better to do" cannot bound discovery on its own. "Heading for a
send" is `READY_TO_EMAIL` plus the in-flight lookups whose next poll still lands within
`COLLECT_TODAY_HORIZON_S`: the backoff is uncapped by design, so a stalled job must stop claiming
today's headroom or a handful of them wedge the pipeline shut for weeks.

Row 4 is the only **per-campaign** step: building a qualifier dominates the cost of using it, so it
scores the whole `QUALIFIED` pool in one pass and drops the model (`qualifier_for`). There is no
`Lead.is_ranked` column — "worth paying for" is what `READY_TO_FIND_EMAIL` already means. It runs
for **every** campaign, freemium included, so the gate calls `predict_probs` on a `KitQualifier` as
readily as on a `BayesianQualifier` — which is why that method is on the `Qualifier` protocol and
not on one implementation. `promote_to_ready` logs the promotion itself, carrying the score that
justified it (`P(f>0.5)=0.997 ≥ 0.75`), and passes `log=False` so the transition is not printed
twice; the score cannot ride in `reason`, which holds the LLM's qualification rationale.

**The walk is also the daemon's time accounting.** `ROWS` pairs each row with a name, so every
action logs which row fired and how long it took (`[Email Outreach] send a first email — 2.3s`), and
at `debug` every row logs its decision time even when it declines. The steps log what they *did*;
without this a row that spends twenty seconds deciding it has nothing to do says so nowhere. When
no row fires, `_log_idle` prints the pipeline counts against today's headroom at most once every
`IDLE_LOG_INTERVAL_S` — idle is the normal state, so a line per cycle would bury everything else,
and the counts separate *no work* from *work behind a gate*, which look identical from outside and
are entirely different problems.

**Log vocabulary is the operator's, not the schema's.** A row is named for what happens to a lead
(`find & qualify new leads`, not `top_up`), the idle counts say what each group is waiting *on*
(`60 waiting to be ranked`, not `Qualified=60`), and the spend gate is printed as its consequence
(`no send headroom left, so not buying or qualifying`, not `room to spend=False`). Function and
state names belong in the code and the diagrams; a log line is read by someone asking what the
daemon is doing to their pipeline.

Campaigns take turns (`_rotate`, re-read each lap so a campaign created after boot joins). There is
no share, no weight and no allocation: with nothing minted in advance there is no budget to split,
so fairness collapses to whose turn it is.

### Steps

Each takes one entity and returns the next `DealState` or `None`; `cycle._apply` then does **one**
`deal.save()`, so a transition and the fields that justify it commit together. Steps are **total**:
they catch the failures they can actually meet and return an explicit state, so the cycle's
`try/except` is a bug backstop, not a retry policy. A step that wants to wait writes
`deal.not_before` — that is the only retry mechanism there is. `HALTING_ERRORS` (today
`ModelHTTPError`) is the exception: a misconfigured LLM key stops the daemon loudly rather than
being retried every five seconds forever behind an `alive` log line.

1. **`buy_address`** (`emails/steps/lookup.py`) — resolves cheapest-first: an address already on the lead → `READY_TO_EMAIL`; the free hub cache (`contacts.resolve`) → `READY_TO_EMAIL`; else `bettercontact.submit` fires a job, stores `lookup_request_id`, and parks at `FINDING_EMAIL`. Couldn't-submit (no key, API down) stays put — no credit spent, no handle to poll.
2. **`check_lookup`** (same module) — polls `lookup_request_id` once: hit → `READY_TO_EMAIL` + hub give-back; miss → `NO_EMAIL_BETTERCONTACT` (no reason, blank outcome); still-running → double `not_before`. **The only terminal outcomes are the provider's own** — no deadline, no attempt limit. Both were tried: past the deadline the leg abandoned the job and reverted the deal, where the buy step bought a *second* job for the same lead, so a provider outage became a hot resubmit loop (418 submits and 4,512 polls in a week for ~40 leads, none terminating). Doubling makes waiting nearly free (a week costs 17 polls) and refuses to mislabel — a timeout is evidence about the provider, not about the lead. The interval rails at `COLLECT_BACKOFF_MAX_S` (a month) only so `datetime` can still express it.
3. **`send_first_email`** (`emails/steps/send.py`) — the only cold message this person will ever get. Materializes the profile summary, composes with the outreach agent (first-touch branch), sends over SMTP (BCC = `operator_bcc` — the operator's own address on their own campaigns, `None` on freemium), writes the outgoing `ChatMessage`, and spaces the box out (`Mailbox.next_send_at`). Re-checks `sender.suppressed(lead)` immediately before sending: an unsubscribe can land in the seconds an LLM call takes.
4. **`answer_reply`** (`emails/steps/reply.py`) — folds the unanswered inbound messages into `chat_summary`, runs the agent, and executes `send_message` (threaded reply, `In-Reply-To` = latest message, `References` = thread root) / `mark_completed` / `suppress`. **Exempt from the daily cap and from send spacing** — a reply is not cold volume, and answering within minutes of being written to is more human, not less.
5. **`top_up`** (`core/pipeline/top_up.py`) — the one step whose queue is a campaign, because a lead nobody has discovered yet has no row to find. One acquisition move per call, chosen by the qualifier's own cold/explore/exploit strategy (unchanged — see **Qualification ML Pipeline**). Freemium runs none of it: its leads are already in the account and the kit model ranks them, so its top-up is "claim the best unclaimed embedded lead".

### Pacing and capacity

Two independent guards, and both now apply to **first emails only**.

- **Rate** — `Mailbox.next_send_at`, rewritten after each first email as `MIN_SEND_INTERVAL_SECONDS + U[0, JITTER)`. Per box rather than pool-wide: the daily ceiling is already per box, two mailboxes are two sending identities, and one receiver's rhythm says nothing about the other's. Fresh jitter each time — a send exactly every 180s is as machine-shaped as no spacing at all.
- **Volume** — `Mailbox.sent_today()` counts **people first contacted today**: outgoing messages grouped by deal, each deal's *first* one kept if it falls after local midnight, then distinct *leads*. So a reply inside an older thread is free, and one person reached from two campaigns counts once. Derived from the message log, so there is no counter to drift after a crash. The ceiling itself is measured, not configured (`emails/warmth.py`).

`Mailbox.objects.free_for_first_email()` is the picker: under cap, not receiver-paused, spacing
elapsed — most idle box wins.

**What the no-chasing rule deleted.** `next_follow_up_at`, `add_business_hours`, the agent's
`follow_up_hours` and `wait` action, and the whole opener-floor apparatus
(`OPENER_FLOOR_FRACTION`, `_opener_reserve`, `_due_follow_ups`). That floor existed because
follow-ups outranked openers on claim *and* were reserved off the top without bound, so once open
threads accumulate faster than they close the owed count consumes every send — measured on a live
install, 102 follow-ups and 1 first email in a week. None of it is needed once a reply is the
trigger: replies are bounded by how many people write back, and they compete for nothing.

## Opt-out and suppression

Every outbound message advertises a working way to leave, and every opt-out is honoured
permanently. Three legs — advertised, detected, enforced — and each is split in two for a
reason.

**Advertised, two ways on the same message.** `emails/sender.py` puts a
`List-Unsubscribe: <mailto:{local}+unsub@{domain}?subject=unsubscribe>` header on every send
*and* a plain-text "reply unsubscribe" line in the body, between the signature and the
attribution. Each reaches recipients the other misses: the header is what receiving filters
read and what Gmail renders its own unsubscribe button from, the line is what a client showing
no button leaves the recipient with. Both work by the same mechanism — put an exit in front of
someone who wants one, so they take it instead of the spam button. Complaint rate is the
dominant reputation input, which is why this is a **deliverability** feature and not a
paperwork one. Header and body line are built in the same function, so it is not possible to
ship one without the other.

**No postal address.** CAN-SPAM requires a valid physical address in the body of every
commercial message, and this deliberately does not carry one: no major filter documents a rule
that reads it, so it buys nothing against the junking this exists to fix, and the scope was cut
to what moves deliverability. That is a knowing compliance gap for US recipients, recorded here
rather than left to be rediscovered — see the history card
`2026-08-03-p1-e2-list-unsubscribe-and-opt-out` in `openoutreach-docs`.

**The alias assumes Gmail-style plus-addressing.** `s@infra.com` advertises
`s+unsub@infra.com`, which delivers to the same INBOX the daemon already reads — no second
mailbox, no DNS, no web surface. Gmail and Google Workspace route `+` tags to the base
mailbox, and they are both the documented default (`smtp.gmail.com` / `imap.gmail.com`) and
what IceMail provisions, so the assumption holds for the boxes this runs on. It is a real
assumption, not a universal truth: a provider that *rejects* plus-addressing would bounce the
opt-out, and the recipient — believing they unsubscribed — would get the next follow-up and
reasonably hit "spam", which is strictly worse than never advertising the header. Nothing
verifies the alias round-trips at onboarding, so **connecting a non-Gmail box is the case to
check by hand**.

The URI is a **`mailto:`, never an `https:`**. RFC 8058 one-click needs an HTTPS endpoint
accepting POST; the self-hosted daemon has no web surface, and routing unsubscribes through
the hub would make every install call home — against the no-telemetry promise. A `mailto:` is
RFC 2369-compliant, needs no infrastructure, and the daemon already speaks IMAP.
`List-Unsubscribe-Post` is therefore **deliberately absent**: it is only valid alongside an
`https:` URI, and one-click binds bulk senders at 5,000+ messages/day to Gmail — we send ~29.

**Detected, two ways, because there are two kinds of event.** A *client-generated* unsubscribe
(Gmail's own button) mints a fresh message with no `References` and no `In-Reply-To`, so
a threaded reader could never see it: the mail pass (`emails/inbox.py`) finds it
**box-wide** by the `+unsub` alias in the To header. A *worded* unsubscribe ("take me off your
list") threads normally and reaches the outreach agent, which has a `suppress` action beside
send/wait/mark_completed. The two cover each other — the first is robust because the mail comes
from the address we mailed, the second because it matches on **thread**, not address.

The scan is resumable: it reads only UIDs above `Mailbox.unsub_scan_uid` and advances the
cursor to the box's `UIDNEXT - 1`, not to the last *matching* UID (matches are rare, and
anchoring on them would re-search the whole tail every pass). A changed `UIDVALIDITY` means the
server reissued its UIDs, so the cursor now points at unrelated mail and the scan restarts from
0 — trusting it would skip every opt-out (and now every reply) below it, forever. It runs every
`MAIL_PASS_INTERVAL_S` off the cycle (a process-held timestamp, like warmth's daily stamp): the cycle fires every few seconds under
send pacing, so a login per pass would be hundreds a day per box, while a *daily* cadence would
let a full day of sends go out after someone asked us to stop. An unreachable box keeps its
cursor — a network fault is not evidence that there was no mail to read.

**Enforced on `Lead.disqualified`,** not on the deal state. It already means "permanent,
account-level, cross-campaign" and is filtered by eleven candidate queries, so reusing it
inherits eleven correct guards instead of adding eleven that could be missed;
`DealState.UNSUBSCRIBED` is only the record of what happened to *this* deal.
`core.db.leads.suppress_email` suppresses **every** lead holding the address (`Lead.email` has
no unique constraint, matched case-insensitively because a client echoes back whatever casing
it was given) and moves their deals to `UNSUBSCRIBED`, leaving already-closed ones alone so an
opt-out weeks after a thread ended cannot erase how it ended. It is idempotent, which is what
makes a rescanned mailbox free. Belt-and-braces, `sender.suppressed(lead)` re-reads the row
immediately before each send at both call sites: the upstream filters are convention, not
DB-enforced (`core/pipeline/qualify.py`), and the agent runs for seconds with the inbox read on
its way past, so the pool query that selected the deal is already out of date.

A **hard bounce must not** set `disqualified` — that binds to the address, not the person, and
would block a replacement address found later. Suppression is also **per-install**: two
instances hold separate SQLite CRMs, so an unsubscribe in one is invisible to the other.
Defensible (different identities, different mailboxes) but recorded here as a decision rather
than discovered later.

## Qualification ML Pipeline

GPR (sklearn, `ConstantKernel * RBF` inside `Pipeline(StandardScaler, GPR)`) with BALD active
learning, over 384-dim FastEmbed embeddings (`BAAI/bge-small-en-v1.5`) stored on `Lead.embedding`;
per-campaign models persisted in `Campaign.model_blob` (joblib, `compress=3`).

1. **Discovery** feeds the pool as **one counted, add-only walk over keyword sets** (`core/pipeline/select.py`, replacing the retired GP-scored maximal walk). A **node** is a set of `(field, token)` `Keyword` rows; its children are itself plus one more token; there is no remove move, because the frontier is global (every unfired child of every fired node, in one pool) so a shallow node's untried siblings stay reachable without one. Firing a node pages Lead Finder for the conjunction into first-touch `Lead`s via `Lead.discovered_by`. `discovery.filters_for(keywords, headcount)` is the only place a node becomes provider JSON, and it is where the index's three operators are chosen between: tokens in the **same field are joined with a space** (words inside one string AND — the walk's narrowing move, and the generator of the best queries measured: `"founder cto"` counts 9,027 at near-perfect precision), different fields AND as separate keys, and the include-list OR stays **unused** because a union reaches one ~10k window where the same values as separate queries reach one each. The campaign's headcount band rides every node unchanged and is never searched. Dedup is `(campaign, token_key)` (`token_key` = sha256 of the sorted `(field, token)` set, a column because no unique constraint spans an M2M); add-only over three fields makes most nodes reachable several ways, so a node is created once and keeps the parent giving the **highest** estimate.

   **A node's value is arithmetic over labels — no model is involved.** `P̂(node) = (a + 2·P̂(parent)) / (a + b + 2)`, where `a`/`b` are the qualified/rejected leads in the store whose `profile_text` contains all of this node's tokens (`select.LabelStore`, loaded once per pass and held in memory — the store is hundreds of rows, so counting a node is a set-containment scan costing microseconds). That is ordinary Laplace smoothing with the prior pointed at the **parent's rate** rather than at 0.5: the parent supplies the level, the child's own counts move it off, and a thin-evidence child stays near its parent instead of swinging to 0 or 1. **The `LabelStore` counts the campaign's anchors as positives**, and that is what makes the cold phase work at all: expansion only offers a token that has shared a *qualified* profile with the node, so a campaign that has never accepted anybody had no qualified profile, could not grow past its one-token seed nodes, fired queries too broad to qualify anyone, and therefore still had no qualified profile — a closed loop in which the seed's own tokens could never be conjoined into the precise query the walk exists to find. The synthetic ideal profiles are written in `profile_text`'s shape, so they tokenize like any lead and say which words describe the people this campaign wants — the same bargain the GP already takes, on the same evidence, with the same expiry (`BayesianQualifier` retires one stored profile per real acceptance and the field empties once real positives reach `ANCHOR_COUNT`, so the invented evidence thins out of the count at exactly the rate ground truth replaces it and no phase check is needed). They deliberately do **not** feed the vocabulary: an anchor is one flat string with no per-field structure, and splitting it by guess would file `united states` as a job title. Anchors say which words go *together*; only a real lead row says which field a word is searchable in. Selection draws `θ ~ Beta(a + 2·P̂(parent), b + 2·(1 − P̂(parent)))` per frontier node and fires the argmax — Thompson sampling, but the Beta parameters *are* the smoothed estimate, so it is one line with nothing to tune (`select.THOMPSON = False` gives greedy). Width tracks evidence, so an untried node gets tried and a node measured bad three times stops appearing. **The GP no longer selects queries.** Measured head-to-head on ~4,100 parent→child edges with the GP fit on half the labels and every truth measured on the other half, counting wins outright (pearson 0.661 vs 0.450) and the GP adds *nothing* on top of it (0.660); residual anchoring (`P(parent) + λ·GP delta`) is real but worth 0.02 at λ≈0.15 and *worse than doing nothing* at λ=1. The GP remains the qualifier — it is what produces the `a`/`b` this walk counts. There is **no counting-call gate, no phase split, no clause lattice, no maximals, no `EmptyClauseSet`, and no λ**. Per-node state (keyword set, offset, state, `leads_found`, lead count) is inspectable in Django Admin, as is the `Keyword` vocabulary. See the roadmap card `p1-e3-leadfinder-index-semantics-and-query-model-rethink`.

   **Growth is counting, not generation; retirement is a corpus fact, never a model fact.** The vocabulary (`core/pipeline/vocabulary.py`) is simply *the words appearing in profiles the LLM already accepted*, one word per keyword, admitted at **df ≥ 2** over the qualified profiles — a floor that drops 65% of the vocabulary (3,485 → 1,208 tokens on the label store) and loses **zero** good tokens, while removing a singleton tail that is mostly company names and typos and that would otherwise be 56% of the *top* of any embedding-based ranking. It runs every pass: a tokenize-and-count over a few hundred profiles needs no cadence knob and no high-water mark, which is what replaced LLM clause minting (the LLM wrote prose — `Head of Content Strategy` — and every extra word is another AND, so those values were near-empty before being conjoined with anything). A token's **field** is read from the lead-row fields that *are* that axis (`discovery.KEYWORD_SOURCE_FIELDS`, stored per lead in `Lead.source_fields`), keeping the per-field vocabularies nearly disjoint for free; `lead_seniority` is seeded whole from the provider's closed 12-value list and never grown. Expansion offers only tokens that have shared a **qualified** profile with the node (`LabelStore.cooccurring`), which bounds the frontier without a top-K cap and keeps every child a proposition the evidence can speak to. Nothing is ever retired for scoring badly — the qualifier refits constantly and a barren yield is a verdict about a view — so only **emptiness** retires a node, and which kind depends on the offset, because the provider answers `0` for all of them: an empty page at **offset 0** (after one spaced retry) means the index matches nobody → `dead`, and its whole subtree with it, since a superset matches a subset of people; an empty page **below the 10k reach cap** means the vein drained completely → `drained`, subtree pruned too (every match is already a `Lead` here); an empty page **at the cap** means Elasticsearch's `max_result_window`, not the end of the population → `drained` but the **subtree stays**, because adding a token opens a fresh 10k window. The fourth case is not an answer at all: rows empty while `summary.leads_found` is positive is a **transport artifact** (a burst answered a 71-million-lead query with an empty page in 0.0s), and it never retires anything — the old walk wrote those down as "matches nobody", permanently and for every campaign. `search()` returns a `Page(leads, leads_found)` and the count is read **only at offset 0**, because past the end of *any* result set the API reports 0 (at 10,100 for a huge query, at 500 for a 397-row one). **Keyword injection** survives but is now vestigial: `db/leads.create_lead` still embeds a lead as `profile_text + keyword_terms(retrieving node)` while `profile_text` — the LLM qualifier's input — stays clean. Its original job was letting the GP score a never-run query by its keywords; that job is gone, and what keeps it is the vector space itself, since every cached `Lead.embedding` was built this way.

   Each unit of work (`pools._advance`) picks a lead to label by the qualifier's own **explore/exploit** split (`acquisition_mode`, driven by class balance), and that split *is* the whole steering. **Cold phase** (`qualifier.is_cold` — invented positives are still padding the class, which lasts until real acceptances have retired all `ANCHOR_COUNT` of them rather than ending at the first one): do **both** moves every pass, one query in and one label out. Rankings here still lean on the anchors' *guess* at the ICP (below), so no observed signal says a label beats a page or the reverse, and any rule that picked one would be a preference dressed as a policy needing a threshold to tune. Interleaving needs none, and it is what the phase wants anyway: discovery is free, so every page opens a region the next label can be picked across. It can't stall — discovery's return is deliberately ignored, so a saturated frontier or a provider outage still leaves a lead to label; only an empty pool ends the pass. *(Discovery itself no longer has a cold phase: the frontier opens on the ICP seed's tokens, every node is scored the same way from day one, and the label store's base rate stands in for the level a root would have supplied — the empty query is never fired, since it matches everyone and its one 10k window is the provider's famous-company head.)* **The cold phase always exploits**: while any positive is an anchor, the campaign's one goal is *more real positives* — each displaces an invented one, and the last of them ends the phase and makes every downstream ranking real — and the highest-P lead is the one most like the ideal profile. BALD does the opposite, spending each call on the lead the model is most *confused* about, which with invented positives is the lead least like the ICP; a live run picked four in a row at P≈0.25–0.42 and got veterinary services, cybersecurity education, K-12 tutoring and a metaverse PM against a health-and-wellness ICP. (The balance could not have chosen it anyway: while the anchors were held at the rejection count `n_neg > n_pos` was false by construction, so the axis was pinned to BALD for the whole phase.) **Explore** (`neg ≤ pos`, past the cold phase): label the most *informative* lead in the pool (max BALD) with **no gate** — a low-confidence lead is exactly the label that teaches the GP the most, so filtering by confidence here would discard the point of exploring. The GP now ranks on real positives, so labelling *is* the better move; page a node in only when the pool is empty. **Exploit** (`neg > pos`): spend the LLM call on a lead that will actually convert — the strongest lead clearing `min_gp_confidence` (`consumable_candidates`); if none clears it, there is nothing worth qualifying, so `discover` more instead.

   The gate is the **same constant the promote gate uses**, and it belongs to exploit alone: it is a *spend* gate — "will this LLM call buy an email, or just park at QUALIFIED?" — not an "is this pool promising?" judgment. Explore wants labels, not emails, so it never consults the gate. (The earlier design applied the gate in **both** states and so ran BALD over the confidence-*filtered* set — picking the most-uncertain lead from a bucket it had just stripped of uncertain leads; that incoherence is what the explore/exploit split removes.) Two other bars that *were* judgments both failed earlier and are not to be reintroduced (see `top_up.py`'s module docstring): each compared an **out-of-sample** candidate score against a bar drawn from **in-sample** ones, and a fitted GP never puts those two populations on the same scale. **Measured 2026-07-17**: the pool tops out at 0.327 against a 0.9 gate, so exploit rarely fires until many more labels exist — a lead the LLM accepts meanwhile parks at QUALIFIED unemailed; it did its job by contributing a label.

   **The GP ranks leads; it no longer ranks queries.** One model decides *which lead to label next* (`qualifier.acquisition_scores`) and gates the paid lookup (`min_gp_confidence`, read by `promote_to_ready` and by `pools._advance`'s exploit branch — the same constant, read from config in both, so they cannot drift). *Which query to fetch next* is counted, not modelled (`select.py`). The unification the keyword injection used to buy was measured and did not hold: over bare keyword strings the GP's posterior collapses toward its prior mean (median +0.080 against +0.797 for real profiles, because a keyword string sits far from every profile embedding), so no absolute threshold on it is meaningful and its ranking of single tokens is topped by df=1 company names. `min_gp_confidence` is *only* the spend gate on the paid lookup. See the roadmap card `p1-e3-leadfinder-index-semantics-and-query-model-rethink` §13.
2. **Balance-driven selection** — `n_negatives > n_positives` → exploit (highest P); else → explore (highest BALD). Anchors count as positives here, but the balance decides nothing while any of them stand — the cold phase is pinned to exploit (above); both run against a real posterior from the first pass. If `acquisition_scores` still returns None the campaign is *unanchored* (LLM outage, no ICP text) — the degraded path, where selection falls back to `creation_date` order because nothing can rank.
3. **LLM decision** — every qualify decision is an LLM call (`qualify_lead.j2` reading the lead's stored `profile_text`); the GP is used only for candidate selection and the confidence gate.
4. **Rank gate** — `ready_pool.promote_to_ready` promotes `QUALIFIED → READY_TO_FIND_EMAIL` when `P(f>0.5)` exceeds `min_gp_confidence` (0.9), so a paid credit is only ever spent on a ranked lead.

The GP needs ≥2 labels of **both** classes to fit; the daemon warm-starts each campaign's from
`Lead.get_labeled_arrays` at boot. Freemium campaigns use a pre-trained `KitQualifier`
(HuggingFace kit) instead of a warm-started GP.

**Anchors — the cold-phase positives.** A first run has no positives at all: the LLM rejects
everything until the ICP is right, so the label set is single-class and *nothing* fits. That is
not a degraded model but an absent one — BALD, `P(f>0.5)`, the promote gate and the query
selector all go dark together, and the engine walks its whole cold phase blind. So a campaign
with no real positive is seeded with a few synthetic ones: `icp.generate_anchors` has the LLM
invent `ANCHOR_COUNT` (3) ideal-lead profiles from `product_docs + campaign_target`, written in
the shape `discovery.profile_text_for` produces, embedded (`ensure_anchors`) and handed to the GP
via `BayesianQualifier.set_anchors`. Several rather than one so the positive region is outlined
rather than pinned to a single hallucination.

- **Profiles, not the product text.** The space they must land in is one of *lead* embeddings;
  marketing prose about the product embeds nowhere near a row of firmographics and would anchor
  the model where no candidate lives. They are also embedded **without** query terms (unlike a
  discovered lead, whose retrieving query rides its embedding) — an anchor claims what a good
  lead looks like, not which query to run, and folding the seed's keywords in would have
  discovery score the seed highly on the strength of our own guess.
- **They never become leads.** They exist only as GP observations plus an Admin window
  (`CampaignAdmin.phase`) onto what the model currently believes; no `Lead` or `Deal` row is
  created and nobody is emailed. Paid spend still needs a real acceptance: `promote_to_ready`
  only ever reads `QUALIFIED` deals, and a deal is only `QUALIFIED` because the LLM accepted a
  real lead — an anchor can raise a real lead's score, it can never be the lead.
- **Balancing is skipped while any of them stand.** `_balance` caps the majority at 2× the
  minority; against 3 synthetic positives that would subsample hundreds of real rejections down
  to six, discarding nearly everything the campaign has actually learned — and it has nothing to
  do anyway while the padding stands. Their pull is local to their own neighbourhood (RBF
  kernel), which is the shape wanted. Balancing takes over when the last anchor retires, which is
  exactly when both classes are real and can diverge again.
- **They retire one per real acceptance, and that is the whole rule**:
  `BayesianQualifier.anchor_budget = max(0, ANCHOR_COUNT - n_real_positives)`. The anchors are a
  guess at the ICP and the campaign's own accepted leads are what replace it, one for one; the
  padding is gone once ground truth has produced as many positives as the guess did. Retirement
  is newest-first, so the profiles written first — the campaign's opening statement of its ICP —
  are the last to go. Dropping the whole set on the first positive could not converge: it took a
  positive class of dozens to one against a pile of rejections in a single step, re-creating the
  flat posterior anchors exist to prevent, one lead into the campaign's real evidence. The
  one-for-one handover is also what keeps a campaign in lead-search mode long enough to
  accumulate a real positive class instead of pivoting off a single acceptance.
- **The clock is acceptances, never rejections.** The earlier rule counted the gap to the
  negatives (`n_neg - n_real_pos`, topped up by a since-deleted `pools._rebalance_anchors`) so the
  classes would stay level. It collapsed to 0 on a campaign whose *first* verdict was an
  acceptance — 3 anchors dropped at once, leaving a positive class of exactly one, which
  `_balance` then pinned the whole training set to (9 observations fitted on 3, the same P
  returned for every lead). It could not recover either: the top-up ran only under `is_cold`,
  which read the anchor count, so an empty set switched off the only path that could refill it.
  A ratio against the rejections is in any case a bet on the accept rate — it empties only if the
  campaign accepts more leads than it rejects, which no outreach funnel does. The countdown reads
  nothing but `n_real_positives`, so no rule governing the anchors depends on the anchors, and 3
  padding positives are enough to keep the class off 1 while they last.
- **`is_cold` (any anchor still standing, i.e. `n_real_positives < ANCHOR_COUNT`) — not
  `has_real_positive`, not "is it fitted?" — is the engine's phase test.** Retirement is written through to `Campaign.anchor_profiles` /
  `anchor_embeddings`, both because the daemon restores the survivors on boot (`stored_anchors`;
  it never invents more once a real positive exists) and because `select.LabelStore` counts those
  same profiles as positives for the discovery walk — a retirement that lived only in memory
  would leave the walk counting evidence the qualifier had already discarded.

## Django Apps

- **`core`** — Engine: `SiteConfig`, `Campaign` models; the cycle, operator lookup, LLM factory, onboarding, the ML/discovery/qualify pipeline, the two agents, geo, vendored mem0.
- **`emails`** — The email channel. `bettercontact.py` (paid finder: the two-leg `submit(query)→request_id` + `poll_once(request_id)→PollOutcome`, the shared blocking `submit_and_poll` transport used by discovery, `is_configured`, `BetterContactQuery`/`Result`/`PollOutcome`/`Unavailable`); `models.py` (`Mailbox` + `SendVerdict` + the per-box capacity pacing manager + `has_mailbox()`); `icemail.py` (`parse_mailboxes` — the App-Passwords sheet), `smtp.py` (`verify_auth`), `mailbox_setup.py` (`import_mailboxes` → parse → auth-check → store); `sender.py` (`send_email` over SMTP+STARTTLS, threading headers, the `List-Unsubscribe` header + `unsubscribe_address`, `suppressed` — the last send-time gate, `operator_bcc` — the BCC-the-operator policy, own campaigns only, mailbox signature then the opt-out block then the `ATTRIBUTION` line appended to the body; a failed send is recorded as a `SendVerdict` on the way past and re-raised unchanged); `delivery_policy.py` (what the receiver's answer means — `classify(exc)` → `Response`, the `POLICIES` table, `record_failure`); `warmth.py` (`refresh_capacity` / `read_sent_history` / `capacity_from` — the measured per-box daily ceiling); `inbox.py` (`read_mail` — the per-box mail pass: replies threaded to their deals and `+unsub` opt-outs, in one walk); `newsletter.py` (`subscribe_to_newsletter`, Brevo); `steps/` (the four steps: `lookup.buy_address`/`check_lookup`, `send.send_first_email`, `reply.answer_reply`).
- **`crm`** — `Lead` (identity + embedding + email) and `Deal` (`crm/models/lead.py`, `crm/models/deal.py`); also defines `DealState` and `Outcome`.
- **`chat`** — `ChatMessage`, FK to the owning `Deal` (the per-(lead, campaign) conversation; the opener + every reply are rows here).
- **`legacy`** — model-less; migration-history anchor only (see Project Layout).
- **`contacts`** — the central contacts-store client (`service.py`, no models, **not** an installed app) — "the hub" (`hub.openoutreach.app`), logged under the `hub:` prefix. `resolve(lead)` (free read-back before the paid finder) and `contribute(lead, emails, origin)` (give-back, non-EEA only, registers on first use). Both best-effort; an outage or missing token degrades to a no-op.

History note: the engine models (`SiteConfig`/`Campaign`) lived in the pre-pivot channel app
until mid-2026 and were moved to `core` (state-only + table renames); that app was then emptied of
models and renamed `legacy`.

## CRM Data Model

- **SiteConfig** (`core/models.py`) — Singleton (pk=1). `ai_model` (pydantic-ai `provider:model`; valid providers openai/anthropic/google/groq/mistral/cohere/openai_compatible), `llm_api_key`, `llm_api_base` (only for `openai_compatible:*`), `bettercontact_api_key` (blank disables discovery + enrichment), `contacts_api_token`/`contacts_api_url` (token earned on first contribution; blank URL → default hub), `country_code` (ISO-3166 alpha-2 — the only persisted operator setting; drives the email-jurisdiction rules via `core/geo`). `SiteConfig.load()`; `core/llm.get_llm_model()` turns it into a `pydantic_ai.models.Model`.
- **Campaign** (`core/models.py`) — `name` (unique), `users` (M2M to `User`), `product_docs`, `campaign_target`, `booking_link`, `is_freemium`, `seed_public_ids`, `model_blob` (per-campaign GP), `country_code` (the ICP's target country, stamped on discovered leads for the geo-gate), `headcount_min`/`headcount_max` (**the ICP size band** — a fixed constraint riding every discovery query unchanged and never a search axis, since loosening a bound queries off-ICP and the provider fills a half-open band with any-size companies rather than returning nothing; a column rather than keywords because it is a *number* the provider takes as a bare scalar). All set by `icp.generate_seed` on cold start. Discovery state lives in `QueryNode` rows, not on the campaign. *(The `clauses` M2M pool and `discovery_minted_at_qualified` were dropped in `0013`: the clause model and LLM minting are both gone.)*
- **Keyword** (`core/models.py`) — one `(field, token)` pair (`lead_job_title = cto`), globally unique and shared across campaigns. A **single word**, never a phrase: every extra word in a Lead Finder value is another AND (`Manager` → `Content Manager` is a ~300× narrowing), so the multi-word values the old pool held were near-empty before being conjoined with anything. Joining is still how the walk narrows, but it happens at query time against measured feedback, one token per move. `field` is constrained to `discovery.SEARCH_FIELDS`; `token` is deliberately unconstrained (outside `lead_seniority` these are free-text search terms and a token the index lacks is just an empty page). `Keyword.rows_for(pairs)` is the one place rows are minted (get-or-create, idempotent).
- **QueryNode** (`core/models.py`) — one node in the walk: `campaign` FK, `keywords` (M2M) + `token_key` (sha256 of the sorted set, the dedup key), `parent` (self-FK — **the level, not provenance**: a child inherits its parent's measured rate as the prior its own counts move off), `next_offset`, `state` (`frontier` / `fired` / `drained` / `dead`), `leads_found` (the provider's corpus count at offset 0, diagnostic only). Unique on `(campaign, token_key)`. **No value column** — the estimate is counted from the label store every time it is needed (`select.estimate`), so there is no counter to drift, nothing to migrate, and nothing to reconcile after a crash; it is also the *same* estimator before and after firing, which is what makes a bad page self-correcting (a node that looked good from the store and returned nobody useful has its own misses land in the counters that made it look good). `pairs` renders the sorted `(field, token)` tuples; `to_filters()` maps onto provider JSON. *(Replaces `Clause`, `DiscoveryQuery` and `EmptyClauseSet`, all dropped in `0013`/`0014`. The anti-monotone prune survives without a blacklist table: a child is skipped at creation if any `dead` node's keyword set is a subset of it — which is the half of the prune that still works once dedup makes the lattice a DAG rather than a tree.)*
- **Lead** (`crm/models/lead.py`) — Keyed on `profile_url` (unique — the discovery provider's per-person URL, the opaque identity/lookup key, **stored, never fetched**). `country_code` (stamped from the discovery ICP; drives the contacts-store geo-gate; blank → never contributed). `embedding` (384-dim float32 BinaryField, built at discovery). `profile_text` (the firmographic text — headline/location/industry/title/company/company-description, plus seniority, company-industry, location state+country, and company-keywords folded in *when the row carries them* — built from the Lead Finder row at discovery, the LLM qualifier's input; no re-scrape). `email` (the finder result; null = not found/unresolved — populated by the two-leg find_email→collect_email legs or a free hub-cache hit, never on the model itself). `disqualified`. `to_profile_dict()` → `{lead_id, profile_url}`; `embedding_array` for numpy; `get_labeled_arrays(campaign)` → (X, y) for GP warm start (non-FAILED → 1, FAILED+wrong_fit → 0, other FAILED → skipped). Created browserless via `core/db/leads.create_lead(row, country_code)` (or freemium seeds via `core/setup/freemium.py`) — there are no scrape accessors.
- **Deal** (`crm/models/deal.py`) — campaign-scoped (`unique(lead, campaign)`). `state` (`DealState`), `outcome` (`Outcome`), `reason` (free text). **Email fields:** `mailbox` (FK to the sending `Mailbox` — the per-box-cap counting key, reply anchor, sticky thread box), `email_subject` (the opener's subject, reused as "Re: …"), `email_sent_at` (opener audit timestamp), `email_message_id` (the immutable thread root the mail pass matches replies on, indexed because that lookup runs per inbound message), `not_before` (**the only schedule a deal carries** — "do not touch this row before this time", written by the lookup backoff and a deferred purchase, null = always eligible), `lookup_request_id`/`lookup_attempt` (the in-flight paid job and its backoff exponent). `profile_summary` / `chat_summary` (lazy mem0-style JSON fact lists, campaign-scoped). `creation_date`, `update_date`.
- **ChatMessage** (`chat/models.py`) — FK to the owning **Deal** (`related_name="messages"`). `content`, `is_outgoing`, `owner`, `external_id` (message identity for per-deal dedup — the email Message-ID; legacy pre-pivot rows hold the retired channel's message URN), `answer_to`/`topic` (self FKs), `creation_date`. Dedup: `unique(deal, external_id)`. The opener + every reply are rows here; `Mailbox.sent_today()` reads them for the per-box cap — each thread's *first* outgoing message, kept if it falls today, counted over distinct leads.
- **Mailbox** (`emails/models.py`) — one SMTP inbox: `host`/`port` (default `smtp.gmail.com:587`), `imap_host`/`imap_port` (default `imap.gmail.com:993` — the read side for the reply loop, same app password), `username`, `password`, `from_address`, `signature` (the sign-off appended to every send from this box — per box because it is part of the sending identity; **NULL = never asked** and the onboarding `signature` step will ask, `""` = declined and sticks), `daily_limit` (the **measured** warm capacity — see `emails/warmth.py`; re-derived daily from the box's own Sent folder rather than configured, and persisted only because reading it costs an IMAP round trip. Defaults to the floor: it applies to a box that has never been measured, and an unmeasured box is one we know nothing about), `next_send_at` (**the send-spacing clock** — the earliest this box may send its next *first* email, rewritten after each one; per box because the daily ceiling is too, and replies ignore it entirely), `unsub_scan_uid`/`unsub_scan_uidvalidity` (the mail pass's IMAP resume cursor — a UID, not a date, so the resume point is exact; a changed UIDVALIDITY means the server reissued UIDs and restarts the scan rather than silently skipping mail. Both are a cache like `daily_limit`, safe to reset to 0). A row exists only once its credentials pass the import auth-check (no health API). Manager: `remaining_today()` (Σ per-box headroom), `free_for_first_email()` (under cap, not receiver-paused, spacing elapsed — most idle box wins); instance `sent_today()` (people first contacted today from this box), `headroom_today()`, `paused_today()`. `has_mailbox()` is the "email is a viable channel" gate.

- **SendVerdict** (`emails/models.py`) — one receiving server's answer to one failed send: `mailbox`, `response` (`delivery_policy.Response`), `smtp_code` (NULL when no SMTP response was ever received — itself the signal that the failure says nothing about reputation), `detail`, `created_at`. Written only on failure, so every row means something. Stored because the SMTP response is evidence that exists nowhere else and does not survive the exception; everything derived from it (`paused_today`, the growth gate) is computed at read time.

## Key Modules

Paths relative to `openoutreach/`.

- **`core/cycle.py`** — the loop and the hierarchy (see **The Cycle**): `run_daemon`, `run_one_action` (rows 1–6, first match wins), `unanswered_replies` (the follow-up trigger — an inbound message newer than our newest outgoing one), `room_to_send_today` (the one spend gate), `read_mail_if_due` / `refresh_capacities_if_due` (the periodic side-effects), `_apply` (one save per transition), `HALTING_ERRORS`. `CYCLE_SECONDS` is fixed and derived from nothing.
- **`core/operator.py`** — who is running this daemon: `get_active_user()`, `campaigns()` (the cycle's rotation), `self_profile()`, `seller_name()`/`seller_full_name()`. Nothing is cached across calls — both reads are one indexed row, and a cache would only let a renamed operator keep signing with the old name until restart. Replaces the browser era's `OperatorSession`, which by the end held nothing session-like: just the Django `User` and whichever campaign the handler was on (now a real FK on the deal).
- **`discovery.py`** — Lead Finder client and the provider contract. `search(filters, limit, offset)` → `Page(leads, leads_found)`: the rows plus the corpus count from `summary.leads_found`, surfaced **only at offset 0** (past the end of *any* result set the API reports 0). `SEARCH_FIELDS` is the three axes a node may add tokens to — `lead_industry` is absent because it is **inert** (a nonsense value returns the identical count to no filter), `lead_function` because it and `lead_department` are one field under two names whose values are ORed (naming both *widens* the query), and `lead_department` because no lead row carries a department, so no vocabulary could ever grow for it. `filters_for(keywords, headcount)` is the only place a node becomes provider JSON (same-field tokens space-joined = AND; different fields = separate keys; the include-list OR deliberately unused). `KEYWORD_SOURCE_FIELDS` maps each axis to the row fields that *are* that axis, and `source_fields_for(row)` stores exactly those on the Lead. `profile_text_for(row)` builds the qualifier's text from `TEXT_FIELDS`; `keyword_terms(keywords)` is what rides the embedding. A field earns its `TEXT_FIELDS` slot by **varying between leads**: the GP ranks the pool's candidates against each other, so a field constant across them adds nothing however accurate. That test excludes the `company_*` free text — Lead Finder staples a fuzzy-matched company record onto every row (a law firm's founder comes back as Meta, mission statement and all; 1–4 distinct records per 100-row page), so `company_description` (59% of the old text) and `company_keywords` (21%) were 80% of every vector at ~zero bits; `contact_location` is absent from every response. **Changing `TEXT_FIELDS` moves the vector space — every `Lead` must be re-embedded**, and the raw rows are not persisted, so in practice that means re-discovering. `embed_query`/`embed_queries` were removed with the GP-scored walk. Shares `submit_and_poll` with `emails/bettercontact.py`.
- **`core/pipeline/`** — `icp.py` (the two cold-start priors, same inputs, two shapes — `generate_seed`: one LLM pass → the campaign's opening **keywords** and size band. It is the *only* LLM call discovery makes about queries: with no qualified leads there are no profiles to count words from, so the ICP text is the one available source. The spec's phrases are **split into single-word tokens** (the LLM writes `"Head of Growth"`, which Lead Finder reads as three ANDed tokens — narrow enough to be empty before the walk has learned anything), letting measurement decide which pair is worth conjoining; `generate_anchors`/`ensure_anchors`: the ICP as synthetic ideal *profiles*, embedded as the GP's positives so a campaign whose every verdict is a rejection can fit at all, retired one per real acceptance), `vocabulary.py` (`tokenize`/`profile_tokens`, `refresh` — grow the keyword table from qualified leads' `source_fields` at df≥2, `seed_seniorities` — the closed 12-value list, `admitted_keywords`), `select.py` (**the selector, and it is arithmetic**: `LabelStore` (token sets + verdicts, loaded once per pass), `estimate`/`_beta_params` (the parent-smoothed rate), `frontier`/`next_node` (one pool, Thompson draw, argmax), `expand` (add-only children over co-occurring tokens, dead-subset pruned), `seed_frontier`, `advance`/`retire`/`_prune_descendants`, `token_key`), `discover.py` (`discover(campaign, qualifier)`: ensure vocabulary + frontier → draw a node → page it → harvest into first-touch `Lead`s with keyword-injected embeddings and expand its children (`_harvest`), or classify the empty page and retire (`_handle_empty`) and try the next node; `qualifier` is accepted and ignored), `qualify.py` (`run_qualification` / `fetch_qualification_candidates` — reads `Lead.profile_text`, no scrape), `ready_pool.py` (GP gate: `promote_to_ready`, `find_ready_candidate`; `min_gp_confidence` is the spend gate **and nothing else**), `top_up.py` (`top_up` — **one** acquisition move per call, the cold/explore/exploit strategy ported verbatim from the old `pools._advance`; `_consumable_candidates` is the exploit gate — see its module docstring. The `while True` that used to wrap it is gone: the cycle is the loop, and the freemium branch is a single "claim the best unclaimed embedded lead"), `freemium_pool.py` (`find_freemium_candidate`). *(`mint.py` is gone — LLM clause minting was replaced by `vocabulary.py`'s counting.)*
- **`core/ml/`** — `qualifier.py` (`Qualifier` protocol, `BayesianQualifier`, `KitQualifier`, `qualify_with_llm`, `format_prediction`), `embeddings.py` (`embed_text`/`embed_texts`, cached FastEmbed model), `hub.py` (`fetch_kit` + the download/load helpers — the HuggingFace campaign kit).
- **`core/setup/freemium.py`** — `import_freemium_campaign` (adds the Django `User`), `seed_profiles` (seeds get an opaque, platform-shaped `profile_url`, embeddings deferred to discovery), `profile_url_from_slug`.
- **`core/db/leads.py`** — `create_lead(row, country_code)` (persist one Lead Finder row as an embedded Lead, idempotent), `promote_lead_to_deal`, `disqualify_lead`.
- **`core/db/deals.py`** — Deal state ops: `set_profile_state`, the state-pool queries (`get_qualified_profiles`, `get_ready_to_find_email_profiles`, `get_emailable_deals`), `create_disqualified_deal`, `create_freemium_deal`. `_STATE_LOG_STYLE` colors the funnel transitions in the log.
- **`core/db/summaries.py`** — the single mem0-style LLM boundary. `materialize_profile_summary_if_missing(deal)` builds `profile_summary` on first follow-up touch from the lead's stored `profile_text` (**no re-scrape**); `update_chat_summary(deal, new_messages, *, seller_name)` folds newly-read replies into `chat_summary` via `reconcile_facts` (mem0 ADD/UPDATE/DELETE/NONE); an identity binding (`operator.seller_name()`) keeps the LLM from misattributing seller-name greetings in a lead reply. mem0's update prompt is vendored under `core/vendor/mem0/` (no `mem0ai` runtime dep).
- **`core/agents/`** — `prompt.py` (Jinja `render` + the thread-agnostic `base_context`/`_format_facts`), `outreach.py` (`run_outreach_agent` → `OutreachDecision{action, subject?, message?, outcome?}` — **one** agent and **one** prompt for the whole conversation, branching on `is_first_touch` (= no `email_message_id`): the cold open must `send_message` with a `subject`, an in-thread turn only ever runs on a thread the lead has **replied** to (the mail pass already wrote the reply) and picks `send_message`/`mark_completed`/`suppress`. There is no `wait` and no follow-up interval: silence is not a decision the agent makes, it is the absence of work. Single structured LLM call, no tool loop. The prompt runs **Mom Test research, not a pitch** — learn how the lead works today, never sell unprompted).
- **`core/llm.py`** — `get_llm_model()` factory (reads `SiteConfig`, `split_model_id` parses the provider out of `ai_model`, dispatches to the per-provider builder), `build_llm_model` (from explicit creds), `verify_llm_credentials` (one live ping, tenacity-retried, used by onboarding), and `run_agent_sync(coro)` — the sync boundary that drives async pydantic-ai on a dedicated long-lived worker-thread loop (never `Agent.run_sync`, whose anyio portal poisons the caller thread's loop slot; never per-call `asyncio.run`, which closes loops the SDK HTTP clients still reference).
- **`core/geo.py`** — jurisdiction sets + predicates: `is_gdpr_protected` (broad opt-in set, drives the newsletter default) and `is_eea_located` / `EEA_UK_CH` (narrow EEA/UK/CH collection-regime set — the client-side pre-gate for contacts-store contribution; the server re-gates authoritatively). Country codes come from onboarding / the discovery row, never from a scrape.
- **`emails/delivery_policy.py`** — what the receiver's answer to a send *means*. `classify(exc)` reduces an `smtplib` failure to a `Response` (`DEFERRED` / `QUOTA_EXCEEDED` / `BLOCKED` / `REFUSED` / `AUTH_FAILED` / `TRANSPORT`), reading Gmail's **enhanced status** (`5.4.5` vs `5.7.1` — both 550, opposite meanings) rather than the bare code; `POLICIES` maps each onto `from_receiver` / `pause_today` / `needs_operator`; `record_failure` persists the `SendVerdict` and returns the policy. The governing distinction: a 4xx means *too fast right now* and the receiver expects a retry (which the next cycle already provides, spaced by the send pacing), so a sporadic deferral costs no capacity — only `550 5.4.5` (the receiver stating its real ceiling) and `550 5.7.x` (a reputation action) pause the box. `from_receiver` is the load-bearing flag: a dropped socket or a bad password also fails a send but says nothing about standing, and letting either gate growth would mean a flaky network throttling a healthy box. Deliberately **no** retry ladder and no rate threshold — a deferred cold opener is not a message we accepted responsibility for, and capacity needs no explicit cut because a box that sends less leaves less in its Sent folder for `warmth.py` to read back.

- **`emails/warmth.py`** — the measured per-box daily ceiling. `read_sent_history` IMAPs the box's Sent folder (found by its `\Sent` **special-use attribute**, so a localized `[Gmail]/Sent Mail` still resolves), headers only, read-only; `capacity_from` takes the 75th percentile of the days it actually sent (mean is dragged down by idle days, max is set by one anomaly) and applies the growth step when `_receiver_pushed_back` is false; `refresh_capacity` persists it to `Mailbox.daily_limit`, falling back to the stored measurement when the box is unreachable — a network blip must never silently throttle a healthy mailbox — *When* the pool was last measured is a **single process-held date** (`_measured_on`) rather than a column or a per-box map: some limit is needed because the cycle fires every few seconds, but per-box granularity buys nothing — mailboxes are only created during onboarding, which runs before the daemon loop in the same process, so every box is measured on that process's first pass either way. Stamped even when a box could not be reached, so a dead mailbox costs one IMAP timeout a day rather than one per cycle. Reading the **Sent folder** rather than our own `ChatMessage` rows is deliberate: the receiver counts every message the box emits, including a human's mail and any provider warmup traffic, and a ceiling derived from our own ledger alone would be blind to all of it. Refreshed once a day from `cycle.refresh_capacities_if_due`.

- **`core/business_time.py`** — `business_days_between(start, end)`: whole Mon–Fri days elapsed, which is all the agent is told about a thread's age, so a Friday reply answered on Monday reads as one day old rather than three. Public holidays are not modelled (per-country data we don't carry). `add_business_hours` went with the follow-up countdown — nobody is chased, so there is no gap to schedule and the only remaining question is how much working time has passed.
- **`core/logging.py`** — `configure_logging` + `print_banner`; `SILENCED_LOGGERS` quiets urllib3/httpx/pydantic_ai/openai/fastembed/etc.
- **`core/migration_compat.py`** + **`management/commands/migrate.py`** — relabel `linkedin → legacy` in `django_migrations` before Django's consistency check, so pre-pivot installs upgrade with a plain `migrate`.
- **`contacts/service.py`** — the hub client: `resolve(lead)` (free read before the paid finder; `/resolve` returns an `emails[]` list, first taken), `contribute(lead, emails, origin)` (give-back at a fresh paid hit, non-EEA only, registers + mints the token on first use; optionally attaches the cached embedding). Reads `SiteConfig.contacts_api_token`/`contacts_api_url`.

## Configuration

- **`SiteConfig`** (DB singleton) — see CRM Data Model. Editable via Django Admin.
- **`conf.py` send pacing** — `MIN_SEND_INTERVAL_SECONDS` (180) + `SEND_INTERVAL_JITTER_SECONDS` (300): the floor between two **first emails from one box**, jittered across the 3–8 minute band the field converges on. Receivers rate-limit on *burst*, not on the daily total — an unpaced daemon drains a day's first emails at its own loop time (measured: ~11s apart, 40 messages inside one hour), which is several times what Gmail is observed to tolerate and a machine signature besides. Applied in `emails/steps/send.py` and persisted on `Mailbox.next_send_at`, per box rather than pool-wide (the daily ceiling is per box too). Replies are exempt entirely. This bounds the *rate*, not the day. *(There is no longer an active-hours window: it existed to make a browser session look like a human's working day and did not survive the email-first pivot. The daemon runs 24/7.)*
- **`conf.py` collect backoff** — `COLLECT_BACKOFF_BASE_S` (5), `COLLECT_BACKOFF_MAX_S` (30 days), `COLLECT_TODAY_HORIZON_S` (1 day): the `collect_email` poll doubles its delay on every still-running attempt and never gives up — MAX is a representability rail, not a deadline. HORIZON is the separate question of whether an in-flight lookup still counts against *today's* send headroom in `flush_find_email_queue`; past it a stalled lookup stops counting, so a few of them can't wedge the submit drain shut. There is no spend cap — paid `find_email` spend is gated by mailbox send-headroom (`flush_find_email_queue`), so a lookup only fires when its result could be sent today.
- **`conf.py` warm capacity** — `WARM_HISTORY_DAYS` (30), `WARM_GROWTH_FACTOR` (1.5), `WARM_FLOOR_SENDS` (5), `WARM_CEILING_SENDS` (**derived**, not declared — `SECONDS_PER_DAY / MEAN_SEND_INTERVAL_SECONDS`, i.e. 86400/330 = 261 at the current pacing). The per-mailbox daily ceiling is **measured, not declared**: `emails/warmth.py` reads the box's own Sent folder over IMAP, takes the 75th percentile of the days it actually sent, and allows a step above it when the receiver has not pushed back. A fixed number could only be wrong in one of two directions — throttling a box that has carried more for months, or handing a box connected an hour ago a seasoned box's volume. The growth step is multiplicative because the history is self-referential (the Sent folder is largely this daemon's own output), so an additive step would make the measurement a one-way ratchet. The ceiling is a rail, not a target, and it is now **arithmetic on the pacing rather than a deliverability opinion**: a daemon that never sends twice inside one interval cannot exceed a day divided by the mean gap, so the rail is computed from `MIN_SEND_INTERVAL_SECONDS`/`SEND_INTERVAL_JITTER_SECONDS` and moves with them — change the pacing and no stale constant is left restating the old number. It was a declared 50 (the top of the 30–50/day band cold-email practice converges on), a figure this repo never measured and which held a warmed box an order of magnitude below the rate the pacing already allowed, making the two guards redundant instead of complementary. What bounds a young box is the ramp, not the rail: ×1.5 off a measured p75 takes weeks of clean sending to reach it (5 → 7 → 10 → 15 → 22 → 33 → 49 → 73 → 109 → 163 → 261), and one receiver verdict freezes it anywhere along the way. Scale beyond the rail by adding boxes. `Mailbox.daily_limit` keeps its name and becomes the measured value; migration `emails/0004` only drops its old fixed default of 40 to the floor.
- **`conf.py:CAMPAIGN_CONFIG`** — `min_gp_confidence` (the GP rank gate — **only** a spend gate on the paid lookup; it is not a steering signal), `qualification_n_mc_samples` (100), `embedding_model` (`BAAI/bge-small-en-v1.5`). **There is no discovery cadence knob**: growing the vocabulary used to be an LLM call worth rationing (`mint_every_n_qualified`, removed) and is now a tokenize-and-count that simply runs every pass. The walk's only other constant is the df≥2 admission floor, which lives in `pipeline/vocabulary.py` beside the measurement that set it.
- **Prompt templates** (`core/templates/prompts/`) — `icp_filters.j2` (the cold-start ICP → seed keywords + size band), `anchor_profiles.j2`, `qualify_lead.j2`, `outreach_agent.j2` (the whole conversation, both branches). *(`mint_clauses.j2` is gone with LLM clause minting.)*
- **`requirements/`** — `base.txt`, `local.txt`, `production.txt`, `crm.txt` (empty).

## Docker

Multi-stage build from `python:3.12-slim-bookworm` using `uv` (no browser, no VNC).
`compose/openoutreach/Dockerfile`. `BUILD_ENV` arg selects
requirements; data persists in a volume at `/app/data`.

## CI/CD

- `tests.yml` — pytest on push / PRs.
- `deploy.yml` — **on every push to `main`**, and on `v*` tags. Runs `make docker-test`, then builds
  + pushes `ghcr.io/eracle/openoutreach`, then fires a `repository-dispatch` (`image-updated`) at
  `eracle/hub.openoutreach.app`. Image tags: `latest` (default branch only), `sha-<commit>`, and
  semver (`v*` tags only).

  **There is no release gate.** Merging to `main` republishes `:latest` — so code and **schema
  migrations reach anyone pulling `latest` on merge, not on a tag**. No `v*` tag has ever been cut,
  so no semver tag exists and there is nothing pinned to roll back to. `sha-<commit>` tags only the
  **pushed tip**: commits buried inside a multi-commit push never get their own image, so a
  migration can go from unpublished to live in one push.

## Dependencies

`requirements/` files; `uv pip install` for fast installs. No browser/Playwright, no DjangoCRM.

Core: `Django`, `pydantic`, `pydantic-ai-slim` (with `openai`/`anthropic`/`google`/`groq`/`mistral`/`cohere`/`bedrock` extras; `griffe` pinned `<2`), `jinja2`, `pandas`, `termcolor`, `tenacity`, `questionary`, `tendo`, `pyyaml`, `jsonpath-ng`
ML: `scikit-learn`, `fastembed`, `huggingface_hub`, `numpy`/`joblib` (transitive)
