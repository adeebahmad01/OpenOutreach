# openoutreach/core/pipeline/pools.py
"""The qualify/discover engine that feeds the paid email lookup.

``find_candidate`` is the one entry point: it hands back the top lead ready for a
BetterContact credit, doing whatever qualification and discovery it takes to surface
one. It loops over three moves, cheapest first:

1. a lead already sitting in READY_TO_FIND_EMAIL → hand it off;
2. a QUALIFIED lead clearing the spend gate → promote it (``promote_to_ready``);
3. otherwise ``_advance`` — spend one unit of work labelling or discovering — and loop.

``_advance`` is the whole steering, and it is just the qualifier's own explore/exploit
split (``acquisition_mode``, driven by class balance):

- **cold phase** (``is_cold`` — invented positives are still padding the positive class,
  which lasts until real acceptances reach the rejection count, not until the first one)
  — do **both** moves every pass: one query in, one label out. Ranking here still leans
  on the anchors' *guess* at the ICP (``icp.generate_anchors``), so there is no
  observed signal saying a label is worth more than a page or the reverse, and a rule
  that picked one would be a preference dressed as a policy. Interleaving needs no
  threshold to tune, and it is what the phase wants anyway — discovery is free, so every
  page opens a region the label can then be picked across. It cannot stall: discovery's
  return is ignored, so a saturated pool or a provider outage still leaves a lead to
  label.
- **explore** (``neg ≤ pos``, past the cold phase) — label the most *informative* lead in
  the pool (max BALD). No gate: a low-confidence lead is exactly the label that teaches
  the GP the most, so filtering by confidence here would throw away the point of
  exploring. The GP now ranks the pool on real positives, so labelling *is* the better
  move and discovery waits for the pool to run dry (there's always a max-BALD lead
  unless there are no leads at all).
- **exploit** (``neg > pos``) — prefer the strongest lead clearing ``min_gp_confidence``
  (``consumable_candidates``), the one whose qualification will buy an email rather than
  park at QUALIFIED. When none clears the gate, fall back to labelling the best lead
  anyway (gate-free): discover only when the pool is empty.

The gate is ``min_gp_confidence`` — the **same constant** ``promote_to_ready`` uses, so a
lead clearing it in exploit is one the promote gate will then pass. It rations the *paid*
BetterContact credit (``promote_to_ready``, the ``find_email`` leg), **not** the free LLM
call. An under-confident GP that clears the gate on nobody must not stop qualifying — a
gate on labelling would freeze the class balance (discovery adds leads, never labels) and
deadlock: the GP never learns the labels that would lift its confidence past the gate,
while free Lead Finder calls deepen an already-idle pool forever. So exploit keeps
labelling below the gate; it just stops *promoting*. Explore never consults the gate at
all — the earlier design applied it in both states and so ran BALD over the
confidence-filtered set, i.e. picked the most-uncertain lead from a bucket it had just
stripped of uncertain leads.

Discovery is free (Lead Finder bills nothing); the paid BetterContact credit is spent
downstream, in the ``find_email`` task, only on a lead this engine already promoted.
"""
from __future__ import annotations

import logging

import numpy as np

from openoutreach.core.conf import CAMPAIGN_CONFIG
from openoutreach.core.ml.qualifier import BayesianQualifier
from openoutreach.core.pipeline.discover import discover
from openoutreach.core.pipeline.qualify import fetch_qualification_candidates, run_qualification
from openoutreach.core.pipeline.ready_pool import find_ready_candidate, promote_to_ready

logger = logging.getLogger(__name__)


def consumable_candidates(qualifier: BayesianQualifier, candidates: list) -> list:
    """The candidates clearing the spend gate — the ones a qualification can convert.

    Empty means exploit has nothing to convert (so it should widen instead): either
    the GP is unfitted or no lead reaches ``min_gp_confidence``, the same constant the
    promote gate uses.
    """
    if not candidates:
        return []

    X = np.array([c.embedding_array for c in candidates], dtype=np.float64)
    probs = qualifier.predict_probs(X)
    if probs is None:
        return []
    threshold = CAMPAIGN_CONFIG["min_gp_confidence"]
    return [c for c, p in zip(candidates, probs) if p >= threshold]


def _rebalance_anchors(session, qualifier: BayesianQualifier) -> None:
    """Top the invented positives up to the shortfall the real ones leave.

    The anchors start at ``ANCHOR_COUNT`` and the rejections do not stop, so a cold phase
    left alone slides into a class balance of 3-against-hundreds, and the fit sees a
    positive class so outnumbered that the posterior flattens toward "no" everywhere —
    the same blindness the anchors were introduced to remove.

    *(This used to carry a second argument: that the imbalance would flip
    ``acquisition_mode`` to exploit and chase conversions on invented evidence. That is
    now the intended cold-phase behaviour — the fastest route to the next real positive
    is the lead most like the ideal profile — so only the flattening argument remains.
    Keeping the classes level still matters for it.)*

    This is the growing half of one invariant; ``BayesianQualifier._retire_anchors`` is
    the shrinking half. Invented positives fill the gap real ones have not closed
    (``anchor_budget`` — ``n_neg - n_real_pos``): a rejection widens it and is topped up
    here, an acceptance narrows it and retires an anchor there. The set therefore never
    exceeds the shortfall, and the last anchor goes exactly when real positives reach the
    rejection count — where dropping the padding still leaves the positive class the
    larger of the two, so nothing about the fit lurches when it happens.

    Rationed: top up only once the gap is a full ``ANCHOR_COUNT`` wide, so this costs one
    LLM call per ``ANCHOR_COUNT`` rejections rather than one per rejection. Best-effort —
    a failed top-up leaves the existing anchors in place.
    """
    from openoutreach.core.pipeline.icp import ANCHOR_COUNT, ensure_anchors

    budget = qualifier.anchor_budget
    if budget - qualifier.n_anchors < ANCHOR_COUNT:
        return

    anchors = ensure_anchors(session.campaign, minimum=budget)
    if anchors is not None:
        qualifier.set_anchors(anchors)


def _advance(session, qualifier: BayesianQualifier) -> bool:
    """Spend one unit of work — label a lead, discover leads, or (cold) both. Returns
    whether it did.

    Which move is the qualifier's balance-driven acquisition mode; see the module
    docstring. Returns False only when the engine has nothing left to do: nothing worth
    labelling and nothing left to discover.
    """
    # Cold phase — invented positives are still padding the positive class, so rankings
    # rest partly on the anchors' guess at the ICP rather than wholly on what has been
    # observed. Do both moves every pass: one query in, one label out. Nothing here says
    # a label is worth more than a page or the reverse, and a rule that picked one would
    # be a preference dressed as a policy. Discovery is free, and each page opens a region
    # the label can then be picked across, so breadth accumulates for exactly as long as
    # the campaign is part-guessing — which is the point of retiring the anchors one
    # acceptance at a time rather than all at once: the search for more like the first
    # good lead lasts until real evidence actually outweighs the guess. Discovery's return
    # is deliberately ignored: a saturated pool or a provider outage still leaves leads to
    # label, and only an empty pool stalls.
    if qualifier.is_cold:
        _rebalance_anchors(session, qualifier)
        discover(session, qualifier)
        candidates = fetch_qualification_candidates(session)
        if not candidates:
            return False
        return run_qualification(session, qualifier, candidates=candidates) is not None

    mode = qualifier.acquisition_mode()
    candidates = fetch_qualification_candidates(session)

    # Exploit — convert the strongest lead clearing the paid-spend gate. If none
    # clears it, still qualify the best lead we have (gate-free): the gate rations the
    # paid BetterContact credit, not the free LLM call, and labelling is what lifts the
    # GP's confidence so a lead *can* clear it. Discovering instead would freeze the
    # class balance and burn Lead Finder calls on an already-deep idle pool forever.
    if mode == "exploit (p)":
        consumable = consumable_candidates(qualifier, candidates)
        if consumable:
            return run_qualification(session, qualifier, candidates=consumable) is not None
        if candidates:
            return run_qualification(session, qualifier, candidates=candidates) is not None
        return discover(session, qualifier) > 0

    # Explore — label the most informative lead we have (max BALD, no gate). The GP is
    # fitted here, so it ranks the pool and there is a best lead to pick; an empty pool
    # is the one case with no lead to label, so page one in first.
    if not candidates:
        if discover(session, qualifier) <= 0:
            return False
        candidates = fetch_qualification_candidates(session)
    return run_qualification(session, qualifier, candidates=candidates) is not None


def find_candidate(session, qualifier: BayesianQualifier) -> dict | None:
    """Top lead ready for the paid email lookup, or None when the engine stalls.

    Advances the qualify/discover engine until a lead reaches READY_TO_FIND_EMAIL or
    there is nothing left to label or discover.
    """
    while True:
        candidate = find_ready_candidate(session, qualifier)
        if candidate is not None:
            return candidate

        if promote_to_ready(session, qualifier) > 0:
            continue

        if not _advance(session, qualifier):
            return None
