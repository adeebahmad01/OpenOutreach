# Architecture

Detailed module documentation for OpenOutreach. See `CLAUDE.md` for rules and quick reference.

OpenOutreach is a browserless, **email-first** AI sales agent: it learns a campaign's ICP
and runs the whole funnel — **define ICP → discover → qualify → rank → find email → agentic
email** — off licensed data, with no LinkedIn account and no scraping.

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
  core/              # engine app (label: core) — daemon, task queue + scheduler,
                     #   Campaign/SiteConfig/Task models, llm.py, conf.py, onboarding,
                     #   ML (qualifier/embeddings/kit), discovery+qualify pipeline,
                     #   the two agents, db/ helpers, session, geo, management commands,
                     #   vendored mem0
  emails/            # channel app (label: emails) — enrichment (BetterContact), Mailbox +
                     #   import + SMTP/IMAP, sender/inbox, the three task handlers
  crm/               # app (label: crm) — Lead, Deal
  chat/              # app (label: chat) — ChatMessage (the per-Deal conversation)
  legacy/            # model-less app (label: legacy) — migration-history anchor only
  contacts/          # central contacts-store client (service.py only — no models, not an app)
```

Layering: `core` owns orchestration, the ML/discovery/qualify pipeline, and the
channel-agnostic models; the `emails` app owns the enrichment + send/read mechanics and the
task handlers. `core` imports channel code only at wiring points (the daemon's handler map).

**No LinkedIn.** The browser, Voyager API, connect/check_pending, and the `linkedin_cli`
dependency were removed in the email-first pivot. The `legacy` app is intentionally
model-less — it exists only to anchor migration history that `core`/`crm` depend on so
existing installs stay on a forward-only, backward-compatible migration graph (the retired
`LinkedInProfile`/`SearchKeyword`/`ActionLog` models were deleted in `legacy/0012`).

## Entry Flow

`manage.py` — stock Django management entrypoint. Bare `python manage.py` (no subcommand, or a
leading flag) defaults to `rundaemon`.

### `rundaemon` management command (`management/commands/rundaemon.py`)

Startup sequence:
1. **Configure logging** — level from `--verbosity`, banner, noisy third-party loggers silenced (`core/logging.py`).
2. **Ensure DB** — `migrate --no-input` (the custom migrate; see below) + `setup_crm` (idempotent).
3. **Onboard** — if `missing_keys()` is non-empty: interactive wizard on a TTY, else print what's missing and exit (no TTY, no silent partial start).
4. **Create session** — validate `llm_api_key`, resolve the active operator `User`, build an `OperatorSession`, default its campaign to the first one.
5. **Run** — `run_daemon(session)`.

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
- The operator's email **is asked** (the `account` step) and stored as `User.email` — it is the **human's own inbox**, deliberately distinct from the mailbox `from_address` (the sending robot). The newsletter subscribes it and the contacts give-back keys the operator; the daemon BCCs a copy of every send here too on the operator's **own** campaigns, and **never** on a freemium one — the whole rule is `emails.sender.operator_bcc(user, campaign)`, which both `send.py` and `follow_up.py` call instead of deciding for themselves. Freemium outreach is OpenOutreach's own conversation, not the operator's, so there is no copy to give (and a blank `User.email` yields `None`, not an empty `Bcc` header). The `From:` header stays the mailbox `from_address`. `account`'s `is_done()` requires an active staff `User` with a **non-blank** email, so a blank-email account can't short-circuit the address prompt.
- The **signature** is its own step, not a field of the `mailbox` step, and that separation is load-bearing. `mailbox`'s `is_done()` is `has_mailbox()`, so once any box exists the step never runs again — a signature asked *inside* it could only ever reach operators who onboarded after it shipped, and every pre-existing install would send unsigned mail forever with nothing to notice it by (that was the `0002` bug, fixed by `0003`). The step keys on **NULL, not emptiness**: NULL means never asked, `""` means declined and must stick, or a declining operator is re-prompted on every startup. It is appended to every send from that box by `sender._sign` — openers and follow-ups alike, since both go through `send_email` — and `sender._attribute` then adds the always-on `Sent with OpenOutreach https://openoutreach.app` line after it, so the operator's own sign-off never carries the attribution. It lives on `Mailbox` rather than `SiteConfig` because it is part of the sending identity, and it stays editable in the Django Admin. The outreach prompt still forbids the agent from signing its own drafts (`prompts/outreach_agent.j2`), so there is exactly one sign-off.
- `missing_keys()` returns the keys of unsatisfied steps (`campaign`/`llm`/`mailbox`/`signature`/`bettercontact`/`account`), so the daemon knows onboarding is incomplete until every gate passes.
- The newsletter opt-in **default** is jurisdiction-aware (off in GDPR/opt-in countries via `core/geo.is_gdpr_protected`), but an explicit yes always subscribes (lawful consent anywhere). Nothing is persisted in the `account` step until the Legal Notice is accepted.
- The interactive wizard is vendored in `onboarding_wizard.py`: thin `text`/`integer`/`confirm`/`multiline` functions over questionary/prompt_toolkit, each owning its own validation loop and returning a value or `None` (cancel). No external `openoutreach` package dependency.

## Deal State Machine

`crm/models/deal.py:DealState` (OpenOutreach-owned `TextChoices`) is the whole funnel — a lead
is discovered and qualified **without** an email in hand (Lead Finder returns firmographics, not
addresses), so the funnel first *finds* the email and then *talks*:

```
QUALIFIED ─(GP rank gate)─▶ READY_TO_FIND_EMAIL ─(find_email/submit)─▶ FINDING_EMAIL ─(collect_email/poll)─▶ hit:  READY_TO_EMAIL
 discovered + qualified      ranked, awaiting the      provider job in flight;         miss: FAILED (reason="no email")
 (no email yet)              paid lookup               request_id in task payload              │
                            (free hub hit → READY_TO_EMAIL directly, no job)                   ▼
                          READY_TO_EMAIL ──(email opener)──▶ EMAILED ⟲ (agentic follow-up) ──▶ COMPLETED / FAILED
                                                             read replies (IMAP) → agent: send / wait / complete
                                                             send: threaded SMTP reply, re-arm next_follow_up_at
```

- **`READY_TO_FIND_EMAIL`** — passed the **GP confidence gate** (`ready_pool.promote_to_ready` above `min_gp_confidence`); queued for the *paid* lookup (one credit per verified hit). The gate rations spend to leads the model is confident about; the submit leg additionally fires only when there's mailbox send-headroom for the result today.
- **`FINDING_EMAIL`** — a provider job is in flight; the deal is excluded from the candidate pool (so the next submit slot can't re-select it and double-charge) while `collect_email` polls to termination. The job handle + poll backoff live in the **collect task's payload**, never on the deal, so an in-flight lookup rides on the persisted task row and survives a restart.
- **`READY_TO_EMAIL`** — an address exists; queued for the opener. A cheap, **ungated** FIFO send-queue paced by the pool-wide send interval and the per-box measured capacity (no ranking step).
- **`EMAILED`** — the opener has been sent; the agentic follow-up loop reads IMAP replies and decides send/wait/complete, paced by the agent's own `follow_up_hours` — **business** hours, stamped onto `Deal.next_follow_up_at` via `core/business_time.add_business_hours`, so weekends don't count down and no follow-up comes due on a Sat/Sun — until a terminal `COMPLETED`/`FAILED`.

**The paid lookup is a two-leg async handshake** (mirroring the retired connect→check_pending). `find_email` (submit) resolves free-hub-first (hit → `READY_TO_EMAIL` with no job/credit), else fires a provider job and parks at `FINDING_EMAIL`; a couldn't-submit (no key / API down) stays `READY_TO_FIND_EMAIL`. `collect_email` (poll) is then **tri-state**: hit → `READY_TO_EMAIL` (address given back to the hub); **miss** (job terminated, no address) → `FAILED`, `reason="no email"`, **outcome blank** — critically not `wrong_fit`, because the ML labeler reads `FAILED+wrong_fit` as a negative and *skips* every other `FAILED` deal, so a lead we simply couldn't find is ML-skipped, never scored a bad fit; **still running** → chains the next poll with doubled backoff, or past the deadline reverts `FINDING_EMAIL → READY_TO_FIND_EMAIL` for a fresh submit (no credit spent).

`crm/models/deal.py:Outcome` (TextChoices): converted, not_interested, wrong_fit, no_budget,
has_solution, bad_timing, unresponsive, unknown — on `Deal.outcome`. `Lead.disqualified=True` =
permanent account-level exclusion (never given a new deal). LLM qualification rejections =
`FAILED` deals with `wrong_fit` outcome (campaign-scoped). Pre-Deal Lead states are implicit:
url-only (a `Lead` row with a null `embedding`) vs embedded (has an `embedding` + `profile_text`,
awaiting qualification).

*(The LinkedIn connect leg — `READY_TO_CONNECT`/`PENDING`/`CONNECTED`, the connect/check_pending
retry+backoff columns — was removed with the channel. Existing deals stranded at those states are
remapped to `QUALIFIED` on upgrade so they re-enter the email funnel.)*

## Task Queue

Persistent queue backed by the `Task` model. Worker loop in `core/daemon.py`:
`claim_next` (**opportunity-cost order**, see `TaskQuerySet.pending`) →
set campaign on session → RUNNING → dispatch via `_HANDLERS` → COMPLETED/FAILED. A `ModelHTTPError`
from the LLM stops the daemon with a clear config hint; any other exception fails just that task and
continues. Between tasks a `_HumanRhythmBreak` injects random burst/break pauses, and a `Heartbeat`
logs an `alive — …` line so the daemon never goes silent for more than 5 minutes. `reconcile(session)`
runs once before the loop and whenever nothing is due, recovering crash-stale RUNNING tasks and
topping up the drains.

**Priority vs scheduling are separate.** `claim_next` picks the highest-value *due* task —
`follow_up` (a live reply waiting) > `collect_email` (a cheap poll that unblocks a deal) > `email`
(a cold opener) > `find_email` (new *paid* speculative work). `seconds_to_next` sleeps by earliest
`scheduled_at` **alone** (never by priority), so a `find_email` due in 1m never oversleeps behind a
`follow_up` due in 6h.

Rows come in two shapes; both are created only in `core/scheduler.py` (no other module inserts
`Task` rows):

- **Lazy drains** (`find_email`/`email`/`follow_up`) — `payload = {"campaign_id": <id>}` only; the handler resolves a concrete target at run time via one eligibility query. Minted by `flush_*_queue` when there's eligible work under the day's send cap; no pre-materialized schedule.
- **Bound poll** (`collect_email`) — `payload` carries the in-flight lookup's `deal_id`, `provider`, `request_id`, `submitted_at`, and backoff `attempt`. `schedule_collect_email` mints it; it is **self-chaining** (each still-running poll mints its successor), so one live poll exists per lookup — bypassing the drains' single-slot guard by construction.

There is **no spend cap and no Poisson pacing**. Paid `find_email` spend rides on send capacity:

1. **`flush_find_email_queue`** — mints one submit slot when there's mailbox send-headroom for the result *today*: `Mailbox.objects.remaining_today()` minus everything already in the send pipeline (`READY_TO_EMAIL + FINDING_EMAIL`). One slot per call (the handler is the pipeline *pump*, so a batch would fan out discovery). No-op unless a mailbox is connected **and** the finder is configured. The GP gate rations *which* leads qualify; the send cap bounds *how many* lookups ride the pipeline — so we never resolve an email we couldn't send today, and free misses re-open the gate at no cost.
2. **Eager drains** (email legs — no anti-bot rhythm to fake) — `flush_email_queue` emits an immediate slot for every `READY_TO_EMAIL` deal; `flush_follow_up_queue` one for every due `EMAILED` deal. Both capped by pool-wide per-box headroom, no-op while a PENDING task of their type exists. `flush_email_queue` and `flush_find_email_queue` additionally take an `allowance` — the campaign's share of today's openers under the quota (below).
3. **`reconcile(session)`** — recovers stale RUNNING tasks, splits today's opener budget across campaigns (`opener_allowances`), then per campaign runs all three drains and logs realized-vs-target share. Bound `collect_email` polls are self-chaining and not reconciled. Called on startup and whenever the queue has no due task.

**The proportional quota** (`core/quota.py`) is the one place `Campaign.action_fraction` binds — the declared cap on how much of the operator's outreach the maintainer's freemium promo may take. It went unenforced until now: `reconcile` let each campaign fill as much pool headroom as it could, so the realized split was whichever pipeline produced candidates fastest, and freemium wins that race by construction (`freemium_pool` draws from any embedded lead with no deal in its campaign, with no GP confidence gate to clear). Two decisions define it:

- **It counts openers, not sends.** A follow-up on a thread the promo already opened is not new reach, and throttling it would strand a human who replied. The ledger is `Deal.email_sent_at` (stamped once, on the opener) and the governed drain is `email`; `follow_up` sits outside the quota and its due volume is reserved off today's headroom *before* the split.
- **It error-diffuses, it does not cap.** `allocate` hands each slot to the campaign furthest below its weighted share — Bresenham/stride scheduling — so `|sent_c − w_c·total| < 1` holds after *every* send, not just in aggregate. At `f=0.2` the promo is dithered one-in-five through the stream instead of landing 28 near-identical mails from one box in an afternoon, which is also the pattern reputation systems cluster on.

The ledger is derived, never stored (no counter to migrate, nothing to drift after a crash), and read over a trailing `QUOTA_WINDOW_DAYS` window — all-time counting would be unbounded debt, where one overshoot silences a campaign for the life of the install. `weights` gives freemium campaigns their `action_fraction` and splits the rest evenly across the operator's own; with no operator campaign the promo takes everything (work-conserving — holding back the only sendable campaign protects nobody). The allowance also caps `flush_find_email_queue`, so a campaign that may not send today does not buy addresses today either.

**Handlers** (in `emails/tasks/`, signature `handle_*(task, session, qualifiers)`):

1. **`handle_find_email`** (`tasks/find_email.py`) — the **submit** leg. Drives the discovery→qualify→rank chain to one top-ranked `READY_TO_FIND_EMAIL` candidate (freemium campaigns draw from the kit-ranked pool and mint the Deal on the fly), tries the free hub cache (`contacts.resolve`) → hit routes straight to `READY_TO_EMAIL` and queues the opener; hub miss → `bettercontact.submit` fires a job, parks the deal at `FINDING_EMAIL`, and schedules the first `collect_email` poll. No-op with no mailbox; couldn't-submit stays `READY_TO_FIND_EMAIL`.
2. **`handle_collect_email`** (`tasks/collect_email.py`) — the **poll** leg. Polls the payload's `request_id` once (`bettercontact.poll_once`): hit → `READY_TO_EMAIL` + hub give-back + queue the opener; miss → `FAILED reason="no email"`; still-running → chain the next poll with doubled backoff (`COLLECT_BACKOFF_BASE_S·2^attempt`, capped) or, past `COLLECT_DEADLINE_S`, revert to `READY_TO_FIND_EMAIL`. A stale deal (no longer `FINDING_EMAIL`) drops the poll.
3. **`handle_email`** (`tasks/send.py`) — picks the `Mailbox` with the most headroom + the oldest `READY_TO_EMAIL` deal (`core.db.deals.get_emailable_deals`), materializes the profile summary, composes the opener (`core/agents/outreach.py`, first-touch branch), sends over SMTP (`emails/sender.py`; BCC = `operator_bcc(user, campaign)` — the operator's own address on their own campaigns, `None` on freemium), then `_record_sent_email` writes the email fields, the outgoing opener `ChatMessage`, and `state=EMAILED` — send record + state on one row, so no double-send window. `next_follow_up_at` is seeded from the agent's own `follow_up_hours`, counted in business hours (`core/business_time.add_business_hours`).
4. **`handle_follow_up`** (`tasks/follow_up.py`) — picks the oldest due `EMAILED` deal whose bound box has headroom, runs `run_outreach_agent` (reads IMAP replies via `emails/inbox.py`, decides), then executes: `send_message` → threaded SMTP reply (`In-Reply-To` = latest message, `References` = thread root) + re-arm the clock; `mark_completed` → `COMPLETED` with the agent's outcome; `wait` → push `next_follow_up_at` out.

## Qualification ML Pipeline

GPR (sklearn, `ConstantKernel * RBF` inside `Pipeline(StandardScaler, GPR)`) with BALD active
learning, over 384-dim FastEmbed embeddings (`BAAI/bge-small-en-v1.5`) stored on `Lead.embedding`;
per-campaign models persisted in `Campaign.model_blob` (joblib, `compress=3`).

1. **Discovery** feeds the pool as **one GP-scored walk over maximal queries** (`core/pipeline/select.py`, replacing the retired best-first frontier): clauses are the axes, and the primary queries are **maximals** — one value per family, the Cartesian product of the campaign's clause pool. The one loosening is **lazy backoff**: a conjunction that matches nobody enqueues its one-clause-looser generalizations, and every new clause value is **pre-screened** (fetched alone before it composes any maximal, dead values dropped) — see the paragraph below. Each campaign owns many `DiscoveryQuery` nodes (a **set of `Clause` rows plus an `offset`**, **fetched once** into first-touch `Lead`s via `Lead.discovered_by`). A `Clause` is one `(family, value)` pair (`lead_job_title = Founder`), globally unique and shared; `discovery.filters_for` is the only place a clause set becomes provider JSON and raises on a second clause of one family. No include-lists: an OR is strictly dominated, because a filter *moves a ~10k-row window* over a provider-ordered corpus rather than narrowing a set, so five ORed titles share one window where five queries get five, free. Dedup is `(campaign, clause_key, offset)` (`clause_key` = sha256 of the sorted set, a column because no unique constraint spans an M2M). `select.next_query(campaign, qualifier)` enumerates fetchable candidates — the maximal product **plus the one-clause-looser generalizations of every recorded empty** (backoff), deduped by `clause_key`, minus `exhausted` lines and empty-pruned sets — each a **fresh** set (offset 0) or a **deepen** of a fetched non-exhausted one (its next page), and scores candidates the same way: embed the keywords (`discovery.embed_query`) and read the GP's `qualifier.acquisition_scores` (predicted P in exploit mode, BALD in explore). Argmax fetches. Exact-embedding every maximal is too costly once the pool is large, so `_prefilter` first ranks the **whole** pool by a cheap composed score — embed only the pool's few dozen distinct clause phrases, pool them per query (free) — and exact-embeds only the top-K on the live axis (`qualifier.acquisition_mode`): a small K for exploit (a query embedding is ~the mean of its clause embeddings, so composed P tracks the truth and the top is complete by K≈128) and a larger one for explore (BALD rewards posterior *variance*, which doesn't decompose over clauses, so the proxy is weaker — see `PREFILTER_K`). **There is no counted-deal metric and no deepen/visit alternation** — the GP that ranks which lead to label ranks which query to fetch, because a discovered lead carries its retrieving query's keywords in its embedding (keyword injection, below). A vein self-bounds: a maximal empties within the 10k window and is marked `exhausted`. Cold start (GP unfitted → `acquisition_scores` None) falls back to seed-first, fresh-before-deep; and for the whole **cold phase** (`has_real_positive` False) candidates are filtered to offset 0, so no vein is deepened before one has been shown to hold anything. `select.py` also owns the node primitives (`persist_fetched`, `mark_exhausted`, `record_empty`, `clause_key`). The per-node clause set / offset / lead count are inspectable in Django Admin, as is the `Clause` vocabulary.

   **Maximals first; broaden by backoff, grow by minting.** A shorter conjunction is a strictly broader window over the same corpus — a superset of the deeper query's people, drifting toward the provider's famous-company head (`{lead_seniority: founder}` → Meta, Meta, Meta). So maximals lead: the selector fires a full-specificity ICP point before anything looser. It loosens only when a maximal comes back **empty** — **lazy backoff** enqueues that conjunction's one-clause-removed generalizations (dropping **value clauses only**; the headcount band rides every child, since loosening a bound queries off-ICP — the provider fills a half-open or inverted band with any-size companies rather than nothing), and the same GP ranks them, so the walk descends toward the non-empty frontier instead of grinding through a Cartesian product of dead leaves. Backed-off queries harvest like any other (**there is no diagnostic-only fetch**; all queries create leads), and the GP down-weights loose regions on its own — a keyword-sparse query sits at the high-variance edge. Growth (not loosening) still comes from **more clause values** (`mint.py`), never from dropping a clause at composition time: minting reads the **qualified** leads' real titles/seniorities/locations and proposes new clause *values* (a new value widens the product; a new family deepens every maximal), triggered by **throughput** (every `mint_every_n_qualified` new qualified leads, a count on `Campaign.discovery_minted_at_qualified`) or **saturation** (the selector returns `None`) — never a GP-confidence gate, which would deadlock cold start. Every batch of new clauses (the seed and every mint) is **pre-screened** first: each new value is fetched **truly alone** — no headcount band, no sibling clause; the only question is whether the keyword means anything to Lead Finder — its full page harvested, and a 0-support value dropped from the pool and recorded empty **as a size-1 set of the value alone**. Probing the value by itself is what makes that singleton record *sound to write globally* (`EmptyClauseSet` carries no campaign FK): it convicts the value itself, not the value-within-this-band, so one campaign's dry probe doesn't wrongly blacklist a value another campaign's larger size band would find. A singleton prunes every maximal containing the value yet generates no backoff child, so a pruned value can't be resurrected into an un-rankable candidate — so no product slab is ever built on a dead axis and each value is probed exactly once. **Keyword injection** is the mechanism that unifies query- and lead-scoring: `db/leads.create_lead` embeds a lead as `profile_text + clause_terms(retrieving query)` (via `discovery.embed_profile`) while `profile_text` — the LLM qualifier's input — stays clean, so the GP learns query-term → fit as a byproduct of labelling and scores a never-run query by its keywords alone (`discovery.embed_query`, keyword-only, so an unsampled query sits at the sparse edge → high variance → explore). `EmptyClauseSet` is the anti-monotone prune (`a candidate is dead iff some recorded set ⊆ it`): pre-screen feeds it **size-1** empties and backoff feeds **sub-maximal** ones, so the subset test bites *within* one pool immediately — recording the size-1 `{location=Oman}` prunes every maximal containing Oman in one shot (and a maximal recorded empty before a mint still prunes the deeper maximals a later mint grows). Only emptiness lands there, never a barren yield, and it convicts the whole set, never a clause inside it.

   Each unit of work (`pools._advance`) picks a lead to label by the qualifier's own **explore/exploit** split (`acquisition_mode`, driven by class balance), and that split *is* the whole steering. **Cold phase** (`qualifier.has_real_positive` False — nothing has ever qualified): do **both** moves every pass, one query in and one label out. Every ranking in play rests on the anchors' *guess* at the ICP (below), so no observed signal says a label beats a page or the reverse, and any rule that picked one would be a preference dressed as a policy needing a threshold to tune. Interleaving needs none, and it is what the phase wants anyway: discovery is free, so every page opens a region the next label can be picked across. It can't stall — discovery's return is deliberately ignored, so a saturated pool or a provider outage still leaves a lead to label; only an empty pool ends the pass. Discovery also **mints every pass and stays shallow** for the whole phase. Minting every pass because the seed is a single conjunction, so the pool spans one maximal and the GP's ranking of it ranks nothing — breadth is what gives the model a set worth sorting, and it is the cheap half of the work (one LLM call proposes many clause values; one fetch tests one conjunction). Shallow because `select.next_query` offers offset 0 only: no vein has been shown to hold anything, so paging deeper drills on the same guess, and when nothing fresh remains the walk reports saturation and mints again. Minted values are still pre-screened, so the breadth is real — one probe fetch per new value is the price of that. **Explore** (`neg ≤ pos`, past the cold phase): label the most *informative* lead in the pool (max BALD) with **no gate** — a low-confidence lead is exactly the label that teaches the GP the most, so filtering by confidence here would discard the point of exploring. The GP now ranks on real positives, so labelling *is* the better move; page a fresh maximal in only when the pool is empty. **Exploit** (`neg > pos`): spend the LLM call on a lead that will actually convert — the strongest lead clearing `min_gp_confidence` (`consumable_candidates`); if none clears it, there is nothing worth qualifying, so `discover` more instead.

   The gate is the **same constant the promote gate uses**, and it belongs to exploit alone: it is a *spend* gate — "will this LLM call buy an email, or just park at QUALIFIED?" — not an "is this pool promising?" judgment. Explore wants labels, not emails, so it never consults the gate. (The earlier design applied the gate in **both** states and so ran BALD over the confidence-*filtered* set — picking the most-uncertain lead from a bucket it had just stripped of uncertain leads; that incoherence is what the explore/exploit split removes.) Two other bars that *were* judgments both failed earlier and are not to be reintroduced (see `pools.py`'s module docstring): each compared an **out-of-sample** candidate score against a bar drawn from **in-sample** ones, and a fitted GP never puts those two populations on the same scale. **Measured 2026-07-17**: the pool tops out at 0.327 against a 0.9 gate, so exploit rarely fires until many more labels exist — a lead the LLM accepts meanwhile parks at QUALIFIED unemailed; it did its job by contributing a label.

   **The GP does double duty; `min_gp_confidence` does not.** One model ranks *which lead to label next* (`qualifier.acquisition_scores`) and *which query to fetch next* (`select.py`, over candidate maximals' keyword embeddings) — the unification the keyword injection buys. `min_gp_confidence` is *only* the spend gate on the paid lookup (read by `promote_to_ready` and by `pools._advance`'s exploit branch — the same constant, read from config in both, never passed, so they cannot drift). The retired frontier steered discovery on ground-truth node counts *because* the GP ranked at chance on 4 positives (AUC 0.396); the rewrite bets that maximals-first precision plus minting from qualified leads accumulate positives faster, and it degrades gracefully — when the GP has no signal, selection is just seed-first exploration, strictly simpler than the walk it replaced. See the roadmap card `p2-e3-discovery-unified-gp-query-selection`.
2. **Balance-driven selection** — `n_negatives > n_positives` → exploit (highest P); else → explore (highest BALD). Anchors count as positives here, so a cold campaign starts in explore and flips to exploit once rejections outnumber them; both run against a real posterior from the first pass. If `acquisition_scores` still returns None the campaign is *unanchored* (LLM outage, no ICP text) — the degraded path, where selection falls back to `creation_date` order because nothing can rank.
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
  created and nobody is emailed. They also cannot reach paid spend: `promote_to_ready` only ever
  reads `QUALIFIED` deals, a deal is only `QUALIFIED` because the LLM accepted a real lead, and
  that acceptance is precisely what drops the anchors — so no BetterContact credit is ever spent
  while they are in play.
- **Balancing is skipped while they are the only positives.** `_balance` caps the majority at 2×
  the minority; against 3 synthetic positives that would subsample hundreds of real rejections
  down to six, discarding nearly everything the campaign has actually learned. Their pull is
  local to their own neighbourhood anyway (RBF kernel), which is the shape wanted.
- **The class is kept level.** Anchors start at `ANCHOR_COUNT` but rejections do not stop, so a
  cold phase left alone slides to 3-against-hundreds — `acquisition_mode` flips to exploit (chasing
  conversions on entirely invented evidence) and the posterior flattens toward "no" everywhere,
  which is the blindness anchors exist to remove. `pools._rebalance_anchors` runs every cold pass
  and tops the set up to `n_neg + ANCHOR_COUNT` when the rejections pull ahead; `ensure_anchors`
  asks only for the shortfall and shows the model what it has already written, so a top-up widens
  the ideal region instead of restating it. Headroom is why this costs one LLM call per
  `ANCHOR_COUNT` rejections rather than one per rejection.
- **They are dropped on the first real positive** (`update(_, 1)` → `_drop_anchors`), in memory
  and on the campaign, so a campaign carrying anchors is exactly one still waiting for its first
  qualified lead. `has_real_positive` — not "is it fitted?" — is therefore the engine's phase
  test, since with anchors a cold campaign fits from the first pass.

## Django Apps

- **`core`** — Engine: `SiteConfig`, `Campaign`, `Task` models; daemon, scheduler, LLM factory, onboarding, the ML/discovery/qualify pipeline, the two agents, session, geo, vendored mem0.
- **`emails`** — The email channel. `bettercontact.py` (paid finder: the two-leg `submit(query)→request_id` + `poll_once(request_id)→PollOutcome`, the shared blocking `submit_and_poll` transport used by discovery, `is_configured`, `BetterContactQuery`/`Result`/`PollOutcome`/`Unavailable`); `models.py` (`Mailbox` + `SendVerdict` + the per-box capacity pacing manager + `has_mailbox()`); `icemail.py` (`parse_mailboxes` — the App-Passwords sheet), `smtp.py` (`verify_auth`), `mailbox_setup.py` (`import_mailboxes` → parse → auth-check → store); `sender.py` (`send_email` over SMTP+STARTTLS, threading headers, `operator_bcc` — the BCC-the-operator policy, own campaigns only, mailbox signature then the `ATTRIBUTION` line appended to the body; a failed send is recorded as a `SendVerdict` on the way past and re-raised unchanged); `delivery_policy.py` (what the receiver's answer means — `classify(exc)` → `Response`, the `POLICIES` table, `record_failure`); `warmth.py` (`refresh_capacity` / `read_sent_history` / `capacity_from` — the measured per-box daily ceiling); `inbox.py` (`sync_inbox` — IMAP reply-reader); `newsletter.py` (`subscribe_to_newsletter`, Brevo); `tasks/` (the four handlers: find_email, collect_email, send, follow_up).
- **`crm`** — `Lead` (identity + embedding + email) and `Deal` (`crm/models/lead.py`, `crm/models/deal.py`); also defines `DealState` and `Outcome`.
- **`chat`** — `ChatMessage`, FK to the owning `Deal` (the per-(lead, campaign) conversation; the opener + every reply are rows here).
- **`legacy`** — model-less; migration-history anchor only (see Project Layout).
- **`contacts`** — the central contacts-store client (`service.py`, no models, **not** an installed app) — "the hub" (`hub.openoutreach.app`), logged under the `hub:` prefix. `resolve(lead)` (free read-back before the paid finder) and `contribute(session, lead, emails, origin)` (give-back, non-EEA only, registers on first use). Both best-effort; an outage or missing token degrades to a no-op.

History note: the engine models (`SiteConfig`/`Campaign`/`Task`) lived in the LinkedIn app until
mid-2026 and were moved to `core` (state-only + table renames); the LinkedIn app was then emptied
to models and renamed `legacy`.

## CRM Data Model

- **SiteConfig** (`core/models.py`) — Singleton (pk=1). `ai_model` (pydantic-ai `provider:model`; valid providers openai/anthropic/google/groq/mistral/cohere/openai_compatible), `llm_api_key`, `llm_api_base` (only for `openai_compatible:*`), `bettercontact_api_key` (blank disables discovery + enrichment), `contacts_api_token`/`contacts_api_url` (token earned on first contribution; blank URL → default hub), `country_code` (ISO-3166 alpha-2 — the only persisted operator setting; drives the email-jurisdiction rules via `core/geo`). `SiteConfig.load()`; `core/llm.get_llm_model()` turns it into a `pydantic_ai.models.Model`.
- **Campaign** (`core/models.py`) — `name` (unique), `users` (M2M to `User`), `product_docs`, `campaign_target`, `booking_link`, `is_freemium`, `action_fraction`, `seed_public_ids`, `model_blob` (per-campaign GP), `country_code` (the ICP's target country, stamped on discovered leads for the geo-gate; set by `icp.generate_seed` from the LLM spec on cold start), `clauses` (M2M to `Clause` — **the clause pool**: the seed conjunction, one value per family. With one clause per family the pool *is* the seed, so the descent can only broaden it (drop a clause), never OR alternatives in; the pool membership is the campaign's because it is this ICP talking, while the `Clause` rows stay global). Discovery state lives in `DiscoveryQuery` nodes, not on the campaign (the old `icp_filters`/`discovery_offset` cursor was dropped in migration 0007; the seed is regenerated on cold start, then embodied by its fetched nodes).
- **Clause** (`core/models.py`) — one `(family, value)` pair (`lead_job_title = Founder`). Globally unique on `(family, value)` and shared across campaigns: a clause is not campaign-specific, and giving it a campaign would duplicate a fact `DiscoveryQuery` already owns. `family` is constrained to `discovery.FILTER_FAMILIES` (the field names are the provider contract, and an unknown key is silently *dropped* — you get the unfiltered page, with rows, reading as success); `value` is deliberately unconstrained, because outside `lead_seniority` these are free-text search terms and a value the index lacks is just an empty page. `Clause.rows_for(pairs)` is the one place clause rows are minted (get-or-create, idempotent), shared by the pool, a fetched node and a blacklisted set. *(`is_live` — a nullable tri-state written by a dedicated `limit=1` probe sweep — was removed in `0009`: it is the `k=1` case of `EmptyClauseSet` and the two pruned identically, since `{c} ⊆ candidate` iff `c ∈ candidate`.)*
- **EmptyClauseSet** (`core/models.py`) — a conjunction the index matches nobody with, down to one clause: `clauses` (M2M) + `clause_key` (unique). Written by `discover.py`/`_prescreen` when a fetch **at offset 0** comes back empty (a deeper empty page is a vein running out), at any depth — a fired maximal, a backed-off generalization, or a **size-1 pre-screen probe** (the value alone, never band-bundled) — and read as the anti-monotone prune (**a candidate is dead iff some recorded set ⊆ it**). Because backoff and pre-screen record *sub-maximal* empties, the subset test bites within one pool immediately (a recorded size-1 `{location=Oman}` prunes every maximal containing Oman); a maximal recorded empty before a mint also prunes the deeper maximals a later mint grows. Global, no campaign FK: emptiness is a fact about the provider's index. A **barren yield never lands here** — only emptiness, and it convicts the whole set, never a clause inside it.
- **DiscoveryQuery** (`core/models.py`) — one **fetched** node: `campaign` FK, `clauses` (M2M to `Clause`) + `clause_key` (sha256 of the sorted set, the dedup key) + `offset`, `exhausted` (bool — set on every offset of a line whose fetch hit an empty page; excluded from selection). Unique on `(campaign, clause_key, offset)`. **No value column** — which query to fetch is scored by the GP on the candidate's keywords (`select.py`), not stored; the node exists to dedup fetches, track the deepen offset, and stamp `Lead.discovered_by` (which carries the query keywords into the lead's embedding). `clause_pairs` renders the sorted `(family, value)` tuples; `to_filters()` maps onto provider JSON. Only fetched nodes exist — no pending queue, no `parent` provenance.
- **Lead** (`crm/models/lead.py`) — Keyed on `profile_url` (unique — the discovery provider's per-person URL, the opaque identity/lookup key, **stored, never fetched**). `country_code` (stamped from the discovery ICP; drives the contacts-store geo-gate; blank → never contributed). `embedding` (384-dim float32 BinaryField, built at discovery). `profile_text` (the firmographic text — headline/location/industry/title/company/company-description, plus seniority, company-industry, location state+country, and company-keywords folded in *when the row carries them* — built from the Lead Finder row at discovery, the LLM qualifier's input; no re-scrape). `email` (the finder result; null = not found/unresolved — populated by the two-leg find_email→collect_email legs or a free hub-cache hit, never on the model itself). `disqualified`. `to_profile_dict()` → `{lead_id, profile_url}`; `embedding_array` for numpy; `get_labeled_arrays(campaign)` → (X, y) for GP warm start (non-FAILED → 1, FAILED+wrong_fit → 0, other FAILED → skipped). Created browserless via `core/db/leads.create_lead(row, country_code)` (or freemium seeds via `core/setup/freemium.py`) — there are no scrape accessors.
- **Deal** (`crm/models/deal.py`) — campaign-scoped (`unique(lead, campaign)`). `state` (`DealState`), `outcome` (`Outcome`), `reason` (free text). **Email fields:** `mailbox` (FK to the sending `Mailbox` — the per-box-cap counting key, reply anchor, sticky thread box), `email_subject` (the opener's subject, reused as "Re: …"), `email_sent_at` (opener audit timestamp), `email_message_id` (the immutable thread root the IMAP reader matches replies on), `next_follow_up_at` (the agentic-loop cursor — seeded by the opener, re-armed each turn, always a working-day moment). `profile_summary` / `chat_summary` (lazy mem0-style JSON fact lists, campaign-scoped). `creation_date`, `update_date`.
- **Task** (`core/models.py`) — `task_type` (find_email/collect_email/follow_up/email), `status` (pending/running/completed/failed), `scheduled_at`, `payload`, timestamps. `TaskQuerySet.pending()` orders by **opportunity-cost priority** (`follow_up > collect_email > email > find_email`) then oldest `scheduled_at`; `claim_next()` takes the highest-priority *due* task, while `seconds_to_next()` sleeps by earliest `scheduled_at` **alone** (never priority). Composite index on `(status, scheduled_at)`.
- **ChatMessage** (`chat/models.py`) — FK to the owning **Deal** (`related_name="messages"`). `content`, `is_outgoing`, `owner`, `external_id` (message identity for per-deal dedup — the email Message-ID; legacy LinkedIn rows hold a Voyager entityUrn), `answer_to`/`topic` (self FKs), `creation_date`. Dedup: `unique(deal, external_id)`. The opener + every reply are rows here; `Mailbox.sent_today()` counts the outgoing ones for the per-box cap.
- **Mailbox** (`emails/models.py`) — one SMTP inbox: `host`/`port` (default `smtp.gmail.com:587`), `imap_host`/`imap_port` (default `imap.gmail.com:993` — the read side for the reply loop, same app password), `username`, `password`, `from_address`, `signature` (the sign-off appended to every send from this box — per box because it is part of the sending identity; **NULL = never asked** and the onboarding `signature` step will ask, `""` = declined and sticks), `daily_limit` (the **measured** warm capacity — see `emails/warmth.py`; re-derived daily from the box's own Sent folder rather than configured, and persisted only because reading it costs an IMAP round trip. Defaults to the floor: it applies to a box that has never been measured, and an unmeasured box is one we know nothing about). A row exists only once its credentials pass the import auth-check (no health API). Manager: `remaining_today()` (Σ per-box headroom), `least_loaded_under_cap()` (most headroom, so a paused box is excluded from the picker exactly as from the budget); instance `sent_today()` (outgoing ChatMessages on this box's deals since local midnight), `headroom_today()`, `paused_today()`. `has_mailbox()` is the "email is a viable channel" gate.

- **SendVerdict** (`emails/models.py`) — one receiving server's answer to one failed send: `mailbox`, `response` (`delivery_policy.Response`), `smtp_code` (NULL when no SMTP response was ever received — itself the signal that the failure says nothing about reputation), `detail`, `created_at`. Written only on failure, so every row means something. Stored because the SMTP response is evidence that exists nowhere else and does not survive the exception; everything derived from it (`paused_today`, the growth gate) is computed at read time.

## Key Modules

Paths relative to `openoutreach/`.

- **`core/daemon.py`** — worker loop, `Heartbeat` + `_HumanRhythmBreak` pacing, `_build_qualifiers` (per-campaign GP warm-start / freemium `KitQualifier`), freemium kit loading (`fetch_kit` → `import_freemium_campaign` → `seed_profiles`), startup + idle `reconcile`.
- **`core/scheduler.py`** — the only creator of `Task` rows: `flush_find_email_queue` (send-headroom-gated submit drain), `flush_email_queue` / `flush_follow_up_queue` (eager drains), `schedule_collect_email` (the bound, self-chaining poll), `opener_allowances` (today's opener budget split by quota), and `reconcile`. No Poisson pacing or spend cap.
- **`core/quota.py`** — the proportional opener split: `opener_counts` (the window ledger off `Deal.email_sent_at`), `weights` (target shares from `action_fraction`), `allocate` (Bresenham error diffusion), `realized_share` + `log_shares` (the audit). No state of its own.
- **`core/session.py`** — `OperatorSession` (browserless): holds the Django `User`, `campaigns` (cached), `self_profile` (synthesized from the user + `SiteConfig` country — not scraped). `get_active_user()`, `get_or_create_session()`.
- **`discovery.py`** — Lead Finder client: `search(filters, limit, offset)` (ICP search → lead rows, free), `profile_text_for(row)` + `embed_row(row)` (the qualifier's text/vector — one `TEXT_FIELDS` list, folded in only when the row carries them so a sparse row stays short). A field earns its slot by **varying between leads**: the GP ranks the pool's candidates against each other, so a field constant across them adds nothing however accurate. That test excludes the `company_*` free text — Lead Finder staples a fuzzy-matched company record onto every row (a law firm's founder comes back as Meta, mission statement and all; 1–4 distinct records per 100-row page), so `company_description` (59% of the old text) and `company_keywords` (21%, the "bee keeper" soup) were 80% of every vector at ~zero bits; `contact_location` is absent from every response. `contact_headline` is the only field with real per-lead signal, and it is filled ~54% of the time. **Changing this list moves the vector space — every `Lead` must be re-embedded**, and the raw rows are not persisted, so in practice that means re-discovering. Shares `submit_and_poll` with `emails/bettercontact.py`.
- **`core/pipeline/`** — `icp.py` (the two cold-start priors, same inputs, two shapes — `generate_seed`: one LLM pass → the seed conjunction, **one value per family** (scalar `ICPSpec`), folded onto `Campaign.clauses`, the single most-precise starting query and the whole starting pool; `generate_anchors`/`ensure_anchors`: the ICP as synthetic ideal *profiles*, embedded as the GP's positives so a campaign whose every verdict is a rejection can fit at all, dropped on the first real positive), `select.py` (**the selector**: `next_query(campaign, qualifier)` builds the candidate frontier — maximals plus the one-clause-looser generalizations of every recorded empty (`_generalizations`, the backoff), deduped — prefilters it by a cheap composed score on the live axis (`_prefilter`, `PREFILTER_K` — small K for exploit, larger for explore; both proxies normalized by clause count so mixed-depth candidates compare depth-neutrally), exact-embeds only the top-K, scores them via `qualifier.acquisition_scores` and returns the argmax — fresh (offset 0) or deepen (next page); cold start falls back to seed-first, `None` = saturated. Owns `persist_fetched`/`mark_exhausted`/`record_empty`/`clause_key`), `mint.py` (`mint_clauses`: grow the pool from the qualified leads' real attributes, returning the fresh pairs; throughput OR saturation triggers, best-effort on LLM outage), `discover.py` (`discover(session, qualifier)`: seed → **pre-screen** new values (`_prescreen`: fetch each alone, drop 0-support, harvest the rest) → throughput mint → fetch the GP's pick into first-touch `Lead`s with keyword-injected embeddings (`_harvest`) → record the two shapes of empty, an offset-0 empty backing off to generalizations → saturation mint), `qualify.py` (`run_qualification` / `fetch_qualification_candidates` — reads `Lead.profile_text`, no scrape), `ready_pool.py` (GP gate: `promote_to_ready`, `find_ready_candidate`; `min_gp_confidence` is the spend gate **and nothing else**), `pools.py` (`find_candidate` — the loop that surfaces one ready lead: hand off a READY lead → promote a QUALIFIED one clearing the gate → else `_advance` one **explore/exploit** unit of work (BALD-label the pool, or exploit-qualify a gate-clearing lead / `discover`); `consumable_candidates` is the exploit gate — see its module docstring), `freemium_pool.py` (`find_freemium_candidate`).
- **`core/ml/`** — `qualifier.py` (`Qualifier` protocol, `BayesianQualifier`, `KitQualifier`, `qualify_with_llm`, `format_prediction`), `embeddings.py` (`embed_text`/`embed_texts`, cached FastEmbed model), `hub.py` (`fetch_kit` + the download/load helpers — the HuggingFace campaign kit).
- **`core/setup/freemium.py`** — `import_freemium_campaign` (adds the Django `User`), `seed_profiles` (seeds get a LinkedIn-shaped opaque `profile_url`, embeddings deferred to discovery), `profile_url_from_slug`.
- **`core/db/leads.py`** — `create_lead(row, country_code)` (persist one Lead Finder row as an embedded Lead, idempotent), `promote_lead_to_deal`, `disqualify_lead`.
- **`core/db/deals.py`** — Deal state ops: `set_profile_state`, the state-pool queries (`get_qualified_profiles`, `get_ready_to_find_email_profiles`, `get_emailable_deals`), `create_disqualified_deal`, `create_freemium_deal`. `_STATE_LOG_STYLE` colors the funnel transitions in the log.
- **`core/db/summaries.py`** — the single mem0-style LLM boundary. `materialize_profile_summary_if_missing(deal, session)` builds `profile_summary` on first follow-up touch from the lead's stored `profile_text` (**no re-scrape**); `update_chat_summary(deal, new_messages, *, seller_name)` folds newly-read replies into `chat_summary` via `reconcile_facts` (mem0 ADD/UPDATE/DELETE/NONE); an identity binding (`seller_name_from(session)`) keeps the LLM from misattributing seller-name greetings in a lead reply. mem0's update prompt is vendored under `core/vendor/mem0/` (no `mem0ai` runtime dep).
- **`core/agents/`** — `prompt.py` (Jinja `render` + the thread-agnostic `base_context`/`_format_facts`), `outreach.py` (`run_outreach_agent` → `OutreachDecision{action, subject?, message?, outcome?, follow_up_hours}` — **one** agent and **one** prompt for the whole conversation, branching on `is_first_touch` (= no `email_message_id`): the cold open must `send_message` with a `subject`, an in-thread turn reads the IMAP-synced thread + a recency window of verbatim messages and picks `send_message`/`wait`/`mark_completed`. Single structured LLM call, no tool loop. The prompt runs **Mom Test research, not a pitch** — learn how the lead works today, never sell unprompted).
- **`core/llm.py`** — `get_llm_model()` factory (reads `SiteConfig`, `split_model_id` parses the provider out of `ai_model`, dispatches to the per-provider builder), `build_llm_model` (from explicit creds), `verify_llm_credentials` (one live ping, tenacity-retried, used by onboarding), and `run_agent_sync(coro)` — the sync boundary that drives async pydantic-ai on a dedicated long-lived worker-thread loop (never `Agent.run_sync`, whose anyio portal poisons the caller thread's loop slot; never per-call `asyncio.run`, which closes loops the SDK HTTP clients still reference).
- **`core/geo.py`** — jurisdiction sets + predicates: `is_gdpr_protected` (broad opt-in set, drives the newsletter default) and `is_eea_located` / `EEA_UK_CH` (narrow EEA/UK/CH collection-regime set — the client-side pre-gate for contacts-store contribution; the server re-gates authoritatively). Country codes come from onboarding / the discovery row, never from a scrape.
- **`emails/delivery_policy.py`** — what the receiver's answer to a send *means*. `classify(exc)` reduces an `smtplib` failure to a `Response` (`DEFERRED` / `QUOTA_EXCEEDED` / `BLOCKED` / `REFUSED` / `AUTH_FAILED` / `TRANSPORT`), reading Gmail's **enhanced status** (`5.4.5` vs `5.7.1` — both 550, opposite meanings) rather than the bare code; `POLICIES` maps each onto `from_receiver` / `pause_today` / `needs_operator`; `record_failure` persists the `SendVerdict` and returns the policy. The governing distinction: a 4xx means *too fast right now* and the receiver expects a retry (which `reconcile` already provides, now spaced by the send pacing), so a sporadic deferral costs no capacity — only `550 5.4.5` (the receiver stating its real ceiling) and `550 5.7.x` (a reputation action) pause the box. `from_receiver` is the load-bearing flag: a dropped socket or a bad password also fails a send but says nothing about standing, and letting either gate growth would mean a flaky network throttling a healthy box. Deliberately **no** retry ladder and no rate threshold — a deferred cold opener is not a message we accepted responsibility for, and capacity needs no explicit cut because a box that sends less leaves less in its Sent folder for `warmth.py` to read back.

- **`emails/warmth.py`** — the measured per-box daily ceiling. `read_sent_history` IMAPs the box's Sent folder (found by its `\Sent` **special-use attribute**, so a localized `[Gmail]/Sent Mail` still resolves), headers only, read-only; `capacity_from` takes the 75th percentile of the days it actually sent (mean is dragged down by idle days, max is set by one anomaly) and applies the growth step when `_receiver_pushed_back` is false; `refresh_capacity` persists it to `Mailbox.daily_limit`, falling back to the stored measurement when the box is unreachable — a network blip must never silently throttle a healthy mailbox — *When* the pool was last measured is a **single process-held date** (`_measured_on`) rather than a column or a per-box map: some limit is needed because `reconcile` fires every few minutes under send pacing, but per-box granularity buys nothing — mailboxes are only created during onboarding, which runs before the daemon loop in the same process, so every box is measured on that process's first pass either way. Stamped even when a box could not be reached, so a dead mailbox costs one IMAP timeout a day rather than one per reconcile. Reading the **Sent folder** rather than our own `ChatMessage` rows is deliberate: the receiver counts every message the box emits, including a human's mail and any provider warmup traffic, and a ceiling derived from our own ledger alone would be blind to all of it. Refreshed once a day from `core/scheduler.reconcile`.

- **`core/business_time.py`** — working-day arithmetic for outreach pacing: `business_days_between(start, end)` (whole Mon–Fri days elapsed, what the agent is told about the thread's age) and `add_business_hours(start, hours)` (advance a countdown in business time — weekend hours don't tick, and a countdown armed on a weekend resumes Monday, so `next_follow_up_at` never expires on a Sat/Sun). Public holidays are not modelled (per-country data we don't carry). Note this is *separate* from send pacing: business time shapes **when a follow-up becomes due**, the send interval shapes **how far apart two sends land**.
- **`core/logging.py`** — `configure_logging` + `print_banner`; `SILENCED_LOGGERS` quiets urllib3/httpx/pydantic_ai/openai/fastembed/etc.
- **`core/migration_compat.py`** + **`management/commands/migrate.py`** — relabel `linkedin → legacy` in `django_migrations` before Django's consistency check, so pre-pivot installs upgrade with a plain `migrate`.
- **`contacts/service.py`** — the hub client: `resolve(lead)` (free read before the paid finder; `/resolve` returns an `emails[]` list, first taken), `contribute(session, lead, emails, origin)` (give-back at a fresh paid hit, non-EEA only, registers + mints the token on first use; optionally attaches the cached embedding). Reads `SiteConfig.contacts_api_token`/`contacts_api_url`.

## Configuration

- **`SiteConfig`** (DB singleton) — see CRM Data Model. Editable via Django Admin.
- **`conf.py` send pacing** — `MIN_SEND_INTERVAL_SECONDS` (180) + `SEND_INTERVAL_JITTER_SECONDS` (300): the pool-wide floor between two sends, jittered across the 3–8 minute band the field converges on. Receivers rate-limit on *burst*, not on the daily total — an unpaced daemon drains a day's openers at its own loop time (measured: ~11s apart, 40 messages inside one hour), which is several times what Gmail is observed to tolerate and a machine signature besides. Applied in `core/scheduler.py:_paced_slots`, pool-wide rather than per-campaign, and covering follow-ups as well as openers. This bounds the *rate*, not the day. *(There is no longer an active-hours window: it existed to make a browser session look like a human's working day and did not survive the email-first pivot. The daemon runs 24/7.)*
- **`conf.py` collect backoff** — `COLLECT_BACKOFF_BASE_S` (5), `COLLECT_BACKOFF_MAX_S` (60), `COLLECT_DEADLINE_S` (600): the `collect_email` poll doubles its delay each still-running attempt (capped at MAX), giving up past DEADLINE. There is no spend cap — paid `find_email` spend is gated by mailbox send-headroom (`flush_find_email_queue`), so a lookup only fires when its result could be sent today.
- **`conf.py` warm capacity** — `WARM_HISTORY_DAYS` (30), `WARM_GROWTH_FACTOR` (1.5), `WARM_FLOOR_SENDS` (5), `WARM_CEILING_SENDS` (50). The per-mailbox daily ceiling is **measured, not declared**: `emails/warmth.py` reads the box's own Sent folder over IMAP, takes the 75th percentile of the days it actually sent, and allows a step above it when the receiver has not pushed back. A fixed number could only be wrong in one of two directions — throttling a box that has carried more for months, or handing a box connected an hour ago a seasoned box's volume. The growth step is multiplicative because the history is self-referential (the Sent folder is largely this daemon's own output), so an additive step would make the measurement a one-way ratchet. The ceiling is a rail, not a target: measurement decides where in the evidenced 30–50 band a box sits, but nothing measured argues it past the band. Scale beyond it by adding boxes. `Mailbox.daily_limit` keeps its name and becomes the measured value; migration `emails/0004` only drops its old fixed default of 40 to the floor.
- **`conf.py:CAMPAIGN_CONFIG`** — `min_gp_confidence` (0.9, the GP rank gate — **only** a spend gate; it is not a steering signal, and there is no discovery-interleave threshold), `qualification_n_mc_samples` (100), `embedding_model` (`BAAI/bge-small-en-v1.5`), and the human-rhythm knobs `burst_min/max_seconds` (45–65 min), `break_min/max_seconds` (10–20 min).
- **Prompt templates** (`core/templates/prompts/`) — `icp_filters.j2`, `mint_clauses.j2` (clause minting from qualified leads), `qualify_lead.j2`, `outreach_agent.j2` (the whole conversation, both branches).
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
