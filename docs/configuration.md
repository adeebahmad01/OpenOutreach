# Configuration

Configuration lives in two places: the **`SiteConfig`** DB singleton and per-campaign **`Campaign`** rows (both managed via interactive onboarding or Django Admin), plus a few hardcoded defaults in **`core/conf.py`**. There are no social-network credentials — OpenOutreach is browserless and uses no such account.

## Operator / LLM / keys (`SiteConfig` singleton, pk=1)

Set during onboarding, editable in Django Admin. `SiteConfig` is the single source of truth for keys and the one persisted operator setting (country).

| Field | Description | Default |
|:------|:------------|:--------|
| `ai_model` | pydantic-ai `provider:model` id (e.g. `anthropic:claude-sonnet-4-5-...`); bare `gpt-*`/`claude-*`/`gemini-*` are auto-prefixed. Providers: openai/anthropic/google/groq/mistral/cohere/openai_compatible. | (required) |
| `llm_api_key` | API key for the chosen provider. Live-verified at onboarding. | (required) |
| `llm_api_base` | Base URL — **only** for `openai_compatible:*`. | (none) |
| `bettercontact_api_key` | [BetterContact](https://bettercontact.rocks?fpr=openoutreach) key (affiliate link — no markup to you). Powers **both** Lead Finder discovery **and** work-email enrichment. **Blank disables discovery + enrichment.** | (empty) |
| `contacts_api_token` / `contacts_api_url` | Cross-operator contacts-store token (earned on first contribution) and URL (blank → default hub). | (empty) |
| `country_code` | ISO-3166 alpha-2. The only persisted operator setting — drives the sending-window timezone and the email/GDPR jurisdiction rules. | (from onboarding) |

The operator's own email and name live on the Django `User` (created at onboarding), not on `SiteConfig`.

## Campaign Settings (`Campaign` model)

Managed via Django Admin (`/admin/`) or created during onboarding.

| Field | Type | Description |
|:------|:-----|:------------|
| `product_docs` | text | Product/service description. Feeds ICP generation, qualification, and the outreach agents. |
| `campaign_target` | text | Who you're going after + the outcome. Feeds the same. |
| `booking_link` | string | URL the agent can offer when suggesting a meeting. |
| `is_freemium` | boolean | Freemium campaign (uses `KitQualifier` instead of the per-campaign GP). |
| `action_fraction` | float | Fraction of activity a freemium campaign devotes to the maintainer-configured promotional email campaign. |
| `icp_filters` | JSON | Cached Lead Finder filter spec (`{"filters": …, "country_code": …}`), generated once by an LLM pass. Clear it to regenerate. |
| `discovery_offset` | integer | Page cursor — how far discovery has paged the ICP; advances across cycles/restarts. |
| `model_blob` | binary | The per-campaign trained GP model (joblib). |

## Sending mailboxes (`Mailbox` model)

Each `Mailbox` is one SMTP/IMAP box you own. Boxes are added during onboarding by pasting an **app password** for a mailbox you control (Gmail / Workspace / own-domain SMTP, or a cold-email provider like [IceMail](https://icemail.ai?via=openoutreach)); each is auth-checked (`emails/smtp.py`) before it is stored. A connected mailbox is required — it gates enrichment and is where outreach is sent from.

| Field | Type | Description | Default |
|:------|:-----|:------------|:--------|
| `host` / `port` | string / int | SMTP host/port. | `smtp.gmail.com` / `587` |
| `imap_host` / `imap_port` | string / int | IMAP host/port — the read side for the reply loop (same app password). | `imap.gmail.com` / `993` |
| `username` | string | SMTP/IMAP login (unique). | (required) |
| `password` | string | App password. | (required) |
| `from_address` | string | The `From:` / sending identity. | (required) |
| `daily_limit` | integer | Warm-safe sends/day, enforced per box at send time. | `DEFAULT_EMAIL_DAILY_LIMIT` (40) |

Sending is raw `smtplib` (`emails/sender.py`); reply-reading is IMAP (`emails/inbox.py`). The email queue drains eagerly, capped only by the pool-wide per-box daily headroom.

## Newsletter jurisdiction default

At onboarding you enter your `country_code`. If it is **not** an opt-in jurisdiction (EU/EEA, UK, Switzerland, Canada, Brazil, Australia, Japan, South Korea, New Zealand), the newsletter default is on; otherwise it is off. An explicit yes always subscribes. The check reads `core/geo.is_gdpr_protected` — country comes from onboarding, never from any account lookup.

## Hardcoded Defaults (`core/conf.py`)

Not user-configurable per campaign; edit the source to change.

| Key | Value | Description |
|:----|:------|:------------|
| `SEND_WINDOW_START_HOUR` / `SEND_WINDOW_END_HOUR` | `8` / `20` | The hours a **first email** may leave, in the operator's local time (start inclusive, end exclusive). Weekends are shut. Gates cold sends only — discovery, paid lookups and the mail pass run 24/7, so replies are still read and answered out of hours. The timezone is resolved from `SiteConfig.country_code`; multi-zone countries take the first zone `pytz` lists. |
| `MIN_SEND_INTERVAL_SECONDS` | `180` | Floor between two first emails **from one box**. Replies are exempt. |
| `SEND_INTERVAL_JITTER_MIN_SECONDS` / `SEND_INTERVAL_JITTER_MAX_SECONDS` | `30` / `90` | Added as `U[min, max]` on top of the floor → a 3.5–4.5 min realized gap. |
| `WARM_HISTORY_DAYS` / `WARM_GROWTH_FACTOR` / `WARM_FLOOR_SENDS` | `30` / `1.5` / `5` | The measured per-box daily ceiling: p75 of the days the box actually sent over the trailing window, ×1.5 when the receiver has not pushed back, never below the floor. `WARM_CEILING_SENDS` is **derived**, not declared — `SEND_WINDOW_SECONDS / MEAN_SEND_INTERVAL_SECONDS` = 43200/240 = **180** at the current settings — so widening the window or the gap moves the rail with it. |
| `COLLECT_BACKOFF_BASE_S` / `COLLECT_BACKOFF_MAX_S` | `5` / `30d` | The `collect_email` poll doubles its delay on every still-running attempt and **never gives up** — an unterminated job is queued, not lost, so the leg keeps the same `request_id` rather than abandoning the deal and paying for a second job. MAX rails the interval only, so the schedule stays representable. |
| `COLLECT_TODAY_HORIZON_S` | `1d` | An in-flight lookup whose next poll is further out than this stops counting against *today's* send headroom in `flush_find_email_queue` — otherwise a few stalled lookups wedge the submit drain shut. |
| `CAMPAIGN_CONFIG.min_gp_confidence` | `0.9` | GP probability threshold for promoting `QUALIFIED → READY_TO_FIND_EMAIL` (rations the paid lookup). |
| `CAMPAIGN_CONFIG.qualification_n_mc_samples` | `100` | Monte Carlo samples for BALD. |
| `CAMPAIGN_CONFIG.embedding_model` | `BAAI/bge-small-en-v1.5` | FastEmbed model for 384-dim embeddings. |

There is **no spend cap and no Poisson pacing** — paid `find_email` spend is gated by mailbox send-headroom, so a lookup only fires when its result could be sent today.

## Working-day pacing

Nobody is chased, so there is no follow-up gap left to schedule and no knob for one.
`core/business_time.py` only *measures* now: it tells the outreach agent a thread's age in **working**
days (`business_days_between`), so a Friday message answered on Monday reads as one day old rather
than three. Public holidays are not modelled — that data is per-country and per-year, and the figure
is coarse enough that a missed holiday costs one slightly-off number in a prompt.

The same Mon–Fri line gates the **sending window** above (`core/sending_window.py`), which decides
when a cold email may leave. The two answer different questions from one calendar: business time
describes *how old a conversation is*, the window decides *when a new one may start*.
