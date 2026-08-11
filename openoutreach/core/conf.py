# openoutreach/core/conf.py
from __future__ import annotations

from pathlib import Path


# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent.parent.parent

PROMPTS_DIR = Path(__file__).parent / "templates" / "prompts"

FASTEMBED_CACHE_DIR = ROOT_DIR / ".cache" / "fastembed"

# ----------------------------------------------------------------------
# Warm capacity (emails/warmth.py) — the per-box daily send ceiling, *measured*
# rather than declared. A fixed number can only ever be wrong in one of two
# directions: it throttles a box that has been carrying more for months, or it
# hands a box connected an hour ago the volume of a seasoned one. So the box
# tells us instead — its own Sent folder is the record of what it has actually
# sustained, which is the only warmth signal available at cold-outreach volume
# (Google Postmaster needs ~100+/day to Gmail before it reports anything).
#
# Read the trailing window's daily counts, take the 75th percentile of the days
# it actually sent (mean is dragged down by idle days, max is set by a single
# anomaly), and allow a step above it. The step is multiplicative because the
# history is *self-referential*: a box's Sent folder is mostly this daemon's own
# output, so an additive step makes the measurement a one-way ratchet — throttled
# to 5/day, a +2 step needs three weeks to climb back, while ×1.5 walks
# 5→9→15→24→38 in under a week. It only applies when the window is clean; a
# failed send holds capacity at what was already demonstrated.
#
# The floor lets a box with no history start somewhere. The ceiling is not a
# number of its own at all — it is *derived* from the send pacing below
# (``WARM_CEILING_SENDS``, defined there because it cannot be stated before the
# interval it divides). It used to be a declared 50, the top of the 30–50/day
# band cold-email practice converges on for a warmed Google Workspace box; that
# figure was folklore this repo never measured, and pinning volume an order below
# the rate made the two guards redundant rather than complementary. What still
# bounds a *young* box is the ramp, not the rail: the ×1.5 step off a measured
# p75 needs weeks of clean sending to reach the rail, and a receiver verdict
# freezes it anywhere along the way.
# ----------------------------------------------------------------------
WARM_HISTORY_DAYS = 30      # trailing window of Sent history to measure
WARM_GROWTH_FACTOR = 1.5    # step above demonstrated volume, when the window is clean
WARM_FLOOR_SENDS = 5        # a box with no history still sends this much
# WARM_CEILING_SENDS — derived from the pacing constants; see below.

# ----------------------------------------------------------------------
# Send pacing (emails/steps/send.py) — the minimum gap between two *first*
# emails from one mailbox. Replies are exempt: a reply is not cold volume, and
# answering someone who wrote to you within minutes is more human, not less.
# Receivers rate-limit on *burst*, not on the daily total: Gmail answers an unusual sending rate with a 421 4.7.0 deferral
# regardless of how far below the daily quota you are. Measured against a real
# box, an unpaced daemon drains a day's openers back-to-back at its own loop
# time (~11s apart, 40 messages inside one hour) — a machine signature, and
# several times the ~20/hour Gmail is observed to tolerate.
#
# The floor is the low end of the 3–8 minute band the cold-email field
# converges on; the jitter spans the rest of it. Jitter is not decoration: a
# send exactly every 180s is as machine-shaped as no spacing at all.
#
# Per box rather than pool-wide (``Mailbox.next_send_at``): the daily ceiling is
# already per box, two mailboxes are two sending identities, and one receiver's
# rhythm says nothing about the other's.
#
# This bounds the *rate*, and the rate sets the day: a daemon that never stops
# and never sends twice inside one interval cannot exceed a day divided by the
# mean interval, whatever any ceiling says. So the warm rail is *computed* from
# these two rather than declared beside them — one number, one place to change
# it. Widen the pacing and the daily rail widens with it; tighten it and the rail
# follows, with no second constant left behind stating the old arithmetic.
#
# The daily measurement still decides where a box sits on its way up — a young
# box is held at its demonstrated volume however much time the clock leaves — but
# it no longer holds a warmed box an order of magnitude below what the pacing
# permits. Burst throttling is the failure mode the interval guards; the ramp
# guards the other one.
# ----------------------------------------------------------------------
MIN_SEND_INTERVAL_SECONDS = 180      # 3 minutes, the hard floor between sends
SEND_INTERVAL_JITTER_SECONDS = 300   # + U[0, 300) → a 3–8 minute spread

SECONDS_PER_DAY = 24 * 60 * 60

# Mean realized gap: the floor plus the expected value of U[0, jitter).
MEAN_SEND_INTERVAL_SECONDS = MIN_SEND_INTERVAL_SECONDS + SEND_INTERVAL_JITTER_SECONDS / 2

# The rail: sends a single box could emit in a day at that mean gap (~261 today).
# Floored, so the rail never claims a send the pacing has no room for.
WARM_CEILING_SENDS = int(SECONDS_PER_DAY / MEAN_SEND_INTERVAL_SECONDS)

# ----------------------------------------------------------------------
# collect_email poll backoff — the bound leg that polls an in-flight paid
# lookup. Each still-running poll doubles the delay (BASE·2^attempt, capped at
# MAX); past DEADLINE the collect leg gives up and reverts the deal to
# READY_TO_FIND_EMAIL for a fresh submit. A provider job resolves in
# seconds-to-minutes, so these are short (unlike the retired channel's
# connect-accept poll, which backed off in hours). A future provider (Apollo, …)
# would carry its own triple.
#
#
# **The backoff is uncapped and there is no deadline.** Both used to exist, and
# together they made the failure worse than the outage: past the deadline the leg
# abandoned the job and reverted the deal to READY_TO_FIND_EMAIL, where the submit
# leg picked it up and paid for a *new* job — a hot resubmit loop against a
# provider already struggling. Measured on a live install during what the provider
# later confirmed was a multi-day infrastructure incident: 418 submits and 4,512
# polls in a week for ~40 leads, none of which ever terminated.
#
# Doubling without a rail fixes both halves. An unterminated job is *queued*, not
# lost, so the right move is to keep the same request_id and ask later — no second
# submit, and the deal stays at FINDING_EMAIL where nothing re-selects it. And
# doubling reaches long waits cheaply: 5s → a week in 17 polls, ~30 for a month.
# A capped backoff would poll a week-long outage 10,000 times to learn the same
# thing. Nothing is ever written off either, which matters because a timeout is
# evidence about the provider, not about the lead — when the queue drains, the
# next poll simply lands and the deal proceeds with no intervention.
# ----------------------------------------------------------------------
COLLECT_BACKOFF_BASE_S = 5

# The rail is on the *interval*, not on the number of attempts: polling stretches
# to a month and then stays there forever. That is not a give-up in disguise —
# nothing is abandoned or relabelled, the job is simply checked monthly instead of
# ever more rarely. It exists because unbounded doubling stops being representable:
# 5s·2^41 is ~348,000 years, `datetime` raises OverflowError, the handler dies
# before minting its successor, and the deal is stranded at FINDING_EMAIL with no
# pending task — the one outcome the whole design is meant to prevent. Past a
# month the difference between "later" and "much later" has no practical meaning
# anyway, while the difference between "later" and "never polled again" is total.
COLLECT_BACKOFF_MAX_S = 30 * 24 * 3600

# A lookup whose next poll is further out than this cannot produce a send today,
# so ``flush_find_email_queue`` stops counting it against today's send headroom —
# otherwise deals parked in a long backoff would slowly wedge the submit drain.
COLLECT_TODAY_HORIZON_S = 24 * 3600

# ----------------------------------------------------------------------
# Campaign config (timing + ML defaults — hardcoded, no YAML)
# ----------------------------------------------------------------------
CAMPAIGN_CONFIG = {
    "qualification_n_mc_samples": 100,
    # GP confidence gate: P(f>0.5) above this promotes QUALIFIED → READY_TO_FIND_EMAIL
    # (rations the paid BetterContact lookup to leads the model is confident about).
    "min_gp_confidence": 0.75,
    # There is no discovery cadence knob. Growing the vocabulary used to be an LLM call
    # worth rationing ("mint_every_n_qualified"); it is now a tokenize-and-count over a
    # few hundred profiles (pipeline/vocabulary.py), so it simply runs every pass. The
    # walk's only other constant is the df≥2 admission floor, which lives with the code
    # that measured it. Discovery steering is arithmetic over labels — no threshold, no
    # confidence gate, no model. See pipeline/select.py.
    "embedding_model": "BAAI/bge-small-en-v1.5",
}


