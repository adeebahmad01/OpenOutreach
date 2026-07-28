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
# The floor lets a box with no history start somewhere; the ceiling is the top of
# the evidenced safe band (sources converge on 30–50/day per warmed Google
# Workspace box), and it is a rail, not a target — measurement decides where in
# the band a box sits, but nothing measured can argue it past the band. Scale
# beyond it by adding mailboxes, never by raising this.
# ----------------------------------------------------------------------
WARM_HISTORY_DAYS = 30      # trailing window of Sent history to measure
WARM_GROWTH_FACTOR = 1.5    # step above demonstrated volume, when the window is clean
WARM_FLOOR_SENDS = 5        # a box with no history still sends this much
WARM_CEILING_SENDS = 50     # hard rail: the top of the evidenced safe band

# ----------------------------------------------------------------------
# Proportional send quota (core/quota.py) — the trailing window the freemium
# ``action_fraction`` is measured over. The ledger is derived from
# ``Deal.email_sent_at``, so this is the only knob: all-time counting would make
# the ledger unbounded debt (one overshoot silences a campaign forever), while a
# window lets the ratio self-heal as history rolls off. 30 days is long enough to
# smooth a stalled week and short enough that a correction lands this month.
# ----------------------------------------------------------------------
QUOTA_WINDOW_DAYS = 30

# ----------------------------------------------------------------------
# Send pacing (core/scheduler.py) — the minimum gap between two outbound
# emails across the whole pool. Receivers rate-limit on *burst*, not on the
# daily total: Gmail answers an unusual sending rate with a 421 4.7.0 deferral
# regardless of how far below the daily quota you are. Measured against a real
# box, an unpaced daemon drains a day's openers back-to-back at its own loop
# time (~11s apart, 40 messages inside one hour) — a machine signature, and
# several times the ~20/hour Gmail is observed to tolerate.
#
# The floor is the low end of the 3–8 minute band the cold-email field
# converges on; the jitter spans the rest of it. Jitter is not decoration: a
# send exactly every 180s is as machine-shaped as no spacing at all.
#
# This bounds the *rate*, not the day: 24h at a 3-minute floor still permits far
# more than any box should send, so the measured warm capacity above remains the
# only thing bounding daily volume. The two guard different failure modes — this
# one burst throttling, that one volume-based spam classification.
# ----------------------------------------------------------------------
MIN_SEND_INTERVAL_SECONDS = 180      # 3 minutes, the hard floor between sends
SEND_INTERVAL_JITTER_SECONDS = 300   # + U[0, 300) → a 3–8 minute spread

# ----------------------------------------------------------------------
# collect_email poll backoff — the bound leg that polls an in-flight paid
# lookup. Each still-running poll doubles the delay (BASE·2^attempt, capped at
# MAX); past DEADLINE the collect leg gives up and reverts the deal to
# READY_TO_FIND_EMAIL for a fresh submit. A provider job resolves in
# seconds-to-minutes, so these are short (unlike the retired LinkedIn
# connect-accept poll, which backed off in hours). A future provider (Apollo, …)
# would carry its own triple.
# ----------------------------------------------------------------------
COLLECT_BACKOFF_BASE_S = 5
COLLECT_BACKOFF_MAX_S = 60
COLLECT_DEADLINE_S = 600  # 10 min

# ----------------------------------------------------------------------
# Campaign config (timing + ML defaults — hardcoded, no YAML)
# ----------------------------------------------------------------------
CAMPAIGN_CONFIG = {
    "qualification_n_mc_samples": 100,
    # GP confidence gate: P(f>0.5) above this promotes QUALIFIED → READY_TO_FIND_EMAIL
    # (rations the paid BetterContact lookup to leads the model is confident about).
    "min_gp_confidence": 0.75,
    # Throughput mint cadence: every N new qualified leads, discovery folds them into
    # the clause pool (mint.py). Not a confidence gate — a count, so it fires at cold
    # start the moment leads qualify. No "is this pool promising?" bar exists here;
    # two were tried and both were constants in disguise (see pools.py). Discovery
    # steering lives on the GP that also ranks leads (select.py), not on a threshold.
    "mint_every_n_qualified": 5,
    "embedding_model": "BAAI/bge-small-en-v1.5",
}


