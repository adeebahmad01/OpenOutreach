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

- **cold start** (``acquisition_mode`` is None — the GP cannot fit) — do **both** moves
  every pass: one query in, one label out. Nothing is ranked in this state, neither the
  leads nor the queries, so there is no signal saying a label is worth more than a page
  or the reverse; a rule that picks one would be a preference dressed as a policy.
  Interleaving needs no threshold to tune and no scale to calibrate, and it is what the
  cold state wants anyway — discovery is free, and every page opens a region the
  coverage walk (``qualify.farthest_from_labelled``) can then pick across, so breadth
  arrives while selection is still a walk rather than a ranking. It also cannot stall:
  discovery's return is ignored, so a saturated pool or a provider outage still leaves
  a lead to label.
- **explore** (``neg ≤ pos``, GP fitted) — label the most *informative* lead in the pool
  (max BALD). No gate: a low-confidence lead is exactly the label that teaches the GP
  the most, so filtering by confidence here would throw away the point of exploring.
  The GP ranks the pool now, so labelling *is* the better move and discovery waits for
  the pool to run dry (there's always a max-BALD lead unless there are no leads at all).
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


def _advance(session, qualifier: BayesianQualifier) -> bool:
    """Spend one unit of work — label a lead, discover leads, or (cold) both. Returns
    whether it did.

    Which move is the qualifier's balance-driven acquisition mode; see the module
    docstring. Returns False only when the engine has nothing left to do: nothing worth
    labelling and nothing left to discover.
    """
    mode = qualifier.acquisition_mode()

    # Cold start — the GP cannot fit, so nothing is ranked: not which lead is worth a
    # label, not which query is worth a fetch. With no signal saying one move beats the
    # other, do both every pass — one query in, one label out. Discovery is free and
    # each page opens a region the label can then be picked across, so the pool grows
    # broad exactly while selection is a coverage walk (``farthest_from_labelled``)
    # rather than a ranking. Discovery's return is deliberately ignored: a saturated or
    # unavailable provider still leaves a pool to label, and only an empty pool stalls.
    if mode is None:
        discover(session, qualifier)
        candidates = fetch_qualification_candidates(session)
        if not candidates:
            return False
        return run_qualification(session, qualifier, candidates=candidates) is not None

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
