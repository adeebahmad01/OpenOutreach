# openoutreach/core/pipeline/qualify.py
"""Qualify orchestration for the lazy chain."""
from __future__ import annotations

import logging

import numpy as np
from termcolor import colored

from openoutreach.core.ml.qualifier import BayesianQualifier

logger = logging.getLogger(__name__)


def fetch_qualification_candidates(session):
    """Embedded, un-dealt Leads awaiting qualification in this campaign, oldest first.

    Invariant (convention, not DB-enforced): a disqualified lead never gets a NEW
    deal, so every deal-creating query filters ``disqualified=False``.
    """
    from openoutreach.crm.models import Lead

    return list(
        Lead.objects.filter(disqualified=False, embedding__isnull=False)
        .exclude(deal__campaign=session.campaign)
        .order_by("creation_date")
    )


# ── cold-start selection ─────────────────────────────────────────────


def farthest_from_labelled(labelled: np.ndarray, embeddings: np.ndarray) -> tuple[int, float]:
    """Index of the candidate farthest from every labelled lead, and that distance.

    Greedy k-center: score each candidate by the distance to its *nearest* labelled
    embedding, take the argmax. One label per call makes the whole cold-start run a
    farthest-point traversal of the pool.

    This is the selector while the GP cannot fit — a run whose labels are all one class
    (every first run, where the LLM rejects everything until the ICP is right) never
    produces a posterior, so neither BALD nor P(f>0.5) is available. The fallback it
    replaces was ``candidates[0]``, i.e. ``creation_date`` order, which spends the whole
    LLM budget walking one query's page of near-identical leads before it ever reaches
    another region. Distance needs no model, so coverage still improves every call: we
    either find the first positive or convict the seed ICP in tens of labels, not
    hundreds.
    """
    cand = np.asarray(embeddings, dtype=np.float64)
    lab = np.asarray(labelled, dtype=np.float64)

    # ‖a-b‖² = ‖a‖² + ‖b‖² - 2a·b — the matmul form, so the (N, M) distance matrix is
    # built without materializing an (N, M, dim) difference tensor.
    d2 = (
        (cand ** 2).sum(axis=1)[:, None]
        + (lab ** 2).sum(axis=1)[None, :]
        - 2.0 * cand @ lab.T
    )
    nearest = np.maximum(d2.min(axis=1), 0.0)  # clamp float error below zero
    best = int(np.argmax(nearest))
    return best, float(np.sqrt(nearest[best]))


def run_qualification(session, qualifier: BayesianQualifier, candidates=None) -> str | None:
    """Qualify one unlabelled profile via the LLM. Returns profile_url or None.

    ``candidates`` restricts the selection to a caller-chosen subset — the consume
    state passes only the leads that can clear the promote gate, so an LLM call is
    never spent on a lead that would park at QUALIFIED. Defaults to the whole
    unlabelled pool, which is what the explore state wants.

    Which candidate gets the call is the qualifier's balance-driven strategy — or, before
    the GP can fit, the farthest-from-labelled pick (``farthest_from_labelled``). The
    verdict itself is always the LLM's.
    """
    from openoutreach.core.ml.qualifier import qualify_with_llm, format_prediction

    if candidates is None:
        candidates = fetch_qualification_candidates(session)
    if not candidates:
        return None

    logger.info(colored("▶ qualify", "blue", attrs=["bold"]))

    # Candidate selection: balance-driven acquisition once the GP fits, coverage before it
    selection_score = None
    if len(candidates) == 1:
        candidate = candidates[0]
    else:
        embeddings = np.array([c.embedding_array for c in candidates], dtype=np.float32)
        result = qualifier.acquisition_scores(embeddings)

        if result is None:
            # Cold start — no posterior yet. Cover the pool instead of walking it in
            # discovery order (``farthest_from_labelled``); with nothing labelled at
            # all there is no reference point, so take the oldest lead as the seed.
            labelled = qualifier.labelled_embeddings
            if len(labelled) == 0:
                candidate = candidates[0]
            else:
                best_idx, distance = farthest_from_labelled(labelled, embeddings)
                candidate = candidates[best_idx]
                selection_score = ("diversity", distance)
                n_neg, n_pos = qualifier.class_counts
                logger.info("Strategy: %s (neg=%d, pos=%d)",
                            colored("diversity (cold start)", "cyan", attrs=["bold"]),
                            n_neg, n_pos)
        else:
            strategy, scores = result
            best_idx = int(np.argmax(scores))
            candidate = candidates[best_idx]
            selection_score = (strategy, float(scores[best_idx]))
            n_neg, n_pos = qualifier.class_counts
            logger.info("Strategy: %s (neg=%d, pos=%d)",
                        colored(strategy, "cyan", attrs=["bold"]), n_neg, n_pos)

    profile_url = candidate.profile_url
    embedding = candidate.embedding_array

    result = qualifier.predict(embedding)

    if result is not None:
        pred_prob, entropy, std = result
        stats = format_prediction(pred_prob, entropy, std, qualifier.n_obs)
        sel = f", {selection_score[0]}={selection_score[1]:.4f}" if selection_score else ""
        logger.debug("%s (%s%s) — querying LLM", profile_url, stats, sel)
    else:
        logger.debug("%s GP not fitted (%d obs) — querying LLM", profile_url, qualifier.n_obs)

    if not candidate.profile_text:
        # A lead we can't read is not a negative fit signal — skip rather than
        # disqualify (e.g. a pre-pivot lead with no persisted firmographic text).
        logger.debug("No profile text for %s — skipping qualification", profile_url)
        return None

    campaign = session.campaign
    label, reason = qualify_with_llm(
        candidate.profile_text,
        product_docs=campaign.product_docs,
        campaign_target=campaign.campaign_target,
    )
    _save_qualification_result(session, qualifier, profile_url, embedding, label, reason)
    return profile_url


def _save_qualification_result(session, qualifier: BayesianQualifier, profile_url: str, embedding: np.ndarray, label: int, reason: str):
    # LLM rejections are tracked as FAILED Deals with "Disqualified" closing reason
    # (campaign-scoped), not as Lead.disqualified (permanent account-level exclusion).
    #
    # A hit leaves the Deal QUALIFIED. The GP rank gate (ready_pool) then promotes
    # it to READY_TO_FIND_EMAIL, where the find_email leg spends a BetterContact
    # credit and routes a hit onward to READY_TO_EMAIL. Enrichment is no longer
    # inline at qualification — it sits behind the rank gate, so a credit is only
    # ever spent on a ranked lead.
    from openoutreach.core.db.deals import create_disqualified_deal
    from openoutreach.core.db.leads import promote_lead_to_deal

    qualifier.update(embedding, label)

    if label == 1:
        try:
            promote_lead_to_deal(session, profile_url, reason=reason)
        except ValueError as e:
            logger.warning("Cannot promote %s: %s — disqualifying", profile_url, e)
            create_disqualified_deal(session, profile_url, reason=str(e))
            return
        logger.info("%s %s: %s", profile_url, colored("QUALIFIED", "green", attrs=["bold"]), reason)
    else:
        create_disqualified_deal(session, profile_url, reason=reason)
