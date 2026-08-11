# Testing

The suite is pytest, mirroring the package structure under `tests/`. Mock at the **boundaries** — the BetterContact client, the hub API, the LLM, and SMTP/IMAP — never inside business logic.

## Running

```bash
make test                 # full suite (local)
make docker-test          # full suite in Docker

.venv/bin/pytest tests/test_qualify.py     # a single file
.venv/bin/pytest -k test_name              # a single test by name
```

## Layout

```
tests/
├── conftest.py                 # shared fixtures: operator (Django User), campaign, stubbed
│                               #   embeddings, and an autouse fixture holding the sending
│                               #   window open (or the send tests would pass by wall clock)
├── factories.py                # factory-boy factories (LeadFactory → profile_url, etc.)
├── agents/test_outreach.py     # the one outreach agent, both ends of the thread
├── contacts/test_service.py    # the hub client (resolve / contribute), best-effort degradation
├── db/
│   ├── test_deals.py           # Deal state ops
│   └── test_summaries.py       # mem0-style profile/chat summaries
├── emails/
│   ├── fake_imap.py            # in-memory IMAP double for the mail pass
│   ├── test_bettercontact.py   # finder submit/poll + discovery transport
│   ├── test_lookup.py          # buy_address → check_lookup, the two-leg paid handshake
│   ├── test_delivery_policy.py # SMTP verdict classification + what each means for the box
│   ├── test_mailbox.py         # per-box daily-cap accounting
│   ├── test_mail_pass.py       # read_mail: threading replies, the UID cursor
│   ├── test_reply.py           # answer_reply — exempt from cap, spacing and window
│   ├── test_send.py            # opener send, the window/cap/spacing gates, EMAILED
│   ├── test_smtp.py            # SMTP auth-check (port-based transport)
│   ├── test_unsubscribe.py     # the +unsub alias, List-Unsubscribe, account-wide suppression
│   └── test_warmth.py          # measured capacity off the Sent folder
├── ml/
│   ├── test_embeddings.py      # FastEmbed embedding
│   └── test_qualifier.py       # GP + BALD selection, LLM qualification
├── test_cycle.py               # the hierarchy: which row fires, and the gates that decline
├── test_sending_window.py      # working-hours gate + country → timezone
├── test_business_time.py       # working-day arithmetic
├── test_discovery.py           # Lead Finder search + embed_row
├── test_discovery_wiring.py    # discover → qualify wiring
├── test_select.py              # the discovery walk: frontier, estimate, expand, retire
├── test_anchors.py             # synthetic ICP positives and their one-per-acceptance retirement
├── test_ready_pool.py          # GP rank gate
├── test_qualify.py             # qualification flow
└── test_onboarding{,_wizard}.py, test_llm.py, test_geo.py, test_db_option.py,
    test_migration_compat.py, test_reset_pipeline.py, test_version.py
```

## Conventions

- **Mock at the boundary.** Patch the BetterContact HTTP client, the hub client, the pydantic-ai model, and SMTP/IMAP transports — not the pipeline functions that call them.
- **CRM objects** come from `factories.py` (factory-boy) or direct model creation.
- **No browser, no network.** There is nothing to launch and no live API to hit; the daemon is browserless and every external call is stubbed.
