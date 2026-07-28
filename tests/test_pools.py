# tests/test_pools.py
"""The qualify/discover engine: ``_advance`` (one explore-or-exploit unit of work)
and ``find_candidate`` (the loop that surfaces a ready lead). Mock
fetch_qualification_candidates, run_qualification, discover, find_ready_candidate,
and promote_to_ready at the pools import site.

The explore/exploit split is the qualifier's ``acquisition_mode`` (mocked directly);
the exploit gate reads ``qualifier.predict_probs`` via ``consumable_candidates``, so
the qualifier is mocked at that boundary too: a score at or above ``min_gp_confidence``
is worth a converting qualification, one below it still gets labelled (gate-free) so the
GP can learn — exploit only discovers when the pool is empty.
"""
from contextlib import contextmanager
from unittest.mock import Mock, patch

import numpy as np

from openoutreach.core.ml.qualifier import BayesianQualifier
from openoutreach.core.pipeline.pools import _advance, find_candidate

PROFILE_URL = "https://www.linkedin.com/in/alice/"
CANDIDATE = {"lead_id": 1, "profile_url": PROFILE_URL, "meta": {}}


def _qualifier(mode, probs=None):
    """A qualifier in ``mode`` ("exploit (p)" / "explore (BALD)" / None) scoring the
    pool at ``probs`` (None is an unfitted GP)."""
    qualifier = Mock()
    qualifier.acquisition_mode.return_value = mode
    qualifier.predict_probs.return_value = (
        None if probs is None else np.asarray(probs, dtype=float)
    )
    return qualifier


@contextmanager
def _engine(candidates, *, qualify=PROFILE_URL, discovered=0, covered=False):
    """Patch the engine's collaborators; yield the (run_qualification, discover) mocks.

    ``covered`` is the explore branch's widen-vs-label signal (``pool_is_covered``),
    patched here because the real one reads embedding geometry off the qualifier.
    """
    with (
        patch("openoutreach.core.pipeline.pools.fetch_qualification_candidates",
              return_value=candidates),
        patch("openoutreach.core.pipeline.pools.pool_is_covered", return_value=covered),
        patch("openoutreach.core.pipeline.pools.run_qualification",
              return_value=qualify) as mock_qualify,
        patch("openoutreach.core.pipeline.pools.discover",
              return_value=discovered) as mock_discover,
    ):
        yield mock_qualify, mock_discover


class TestAdvanceExploit:
    def test_converts_a_lead_that_clears_the_gate(self):
        """A lead above the gate can reach email — qualify only that subset, don't widen."""
        weak = Mock(embedding_array=np.zeros(384))
        strong = Mock(embedding_array=np.ones(384))
        with _engine([weak, strong]) as (mock_qualify, mock_discover):
            with patch.dict("openoutreach.core.pipeline.pools.CAMPAIGN_CONFIG",
                            {"min_gp_confidence": 0.9}):
                assert _advance("session", _qualifier("exploit (p)", probs=[0.3, 0.95])) is True

        assert mock_qualify.call_args.kwargs["candidates"] == [strong]
        mock_discover.assert_not_called()

    def test_labels_the_pool_when_nothing_clears_the_gate(self):
        """No lead clears the paid-spend gate, but the pool is non-empty — label it
        anyway (gate-free) so the GP's confidence can rise; don't burn a discover."""
        lead = Mock(embedding_array=np.zeros(384))
        with _engine([lead], discovered=100) as (mock_qualify, mock_discover):
            with patch.dict("openoutreach.core.pipeline.pools.CAMPAIGN_CONFIG",
                            {"min_gp_confidence": 0.9}):
                assert _advance("session", _qualifier("exploit (p)", probs=[0.3])) is True

        assert mock_qualify.call_args.kwargs["candidates"] == [lead]
        mock_discover.assert_not_called()

    def test_discovers_only_when_the_pool_is_empty(self):
        """Nothing to label at all → discover a page (the sole remaining move)."""
        with _engine([], discovered=100) as (mock_qualify, mock_discover):
            assert _advance("session", _qualifier("exploit (p)", probs=[])) is True

        mock_qualify.assert_not_called()
        mock_discover.assert_called_once()


class TestAdvanceExplore:
    def test_labels_the_whole_pool_with_no_gate(self):
        """Explore hands the LLM the full pool — BALD wants the uncertain lead the gate
        would strip out."""
        pool = [Mock(embedding_array=np.zeros(384)), Mock(embedding_array=np.ones(384))]
        with _engine(pool) as (mock_qualify, mock_discover):
            assert _advance("session", _qualifier("explore (BALD)")) is True

        assert mock_qualify.call_args.kwargs["candidates"] == pool
        mock_discover.assert_not_called()

    def test_cold_start_labels_without_a_gate(self):
        """An unfitted GP (acquisition_mode None) labels like explore — one label moves it."""
        pool = [Mock(embedding_array=np.zeros(384))]
        with _engine(pool) as (mock_qualify, mock_discover):
            assert _advance("session", _qualifier(None)) is True

        assert mock_qualify.call_args.kwargs["candidates"] == pool
        mock_discover.assert_not_called()

    def test_empty_pool_pages_in_then_labels(self):
        """No lead to label → discover a page, then label it."""
        with (
            patch("openoutreach.core.pipeline.pools.fetch_qualification_candidates",
                  side_effect=[[], [Mock(embedding_array=np.zeros(384))]]),
            patch("openoutreach.core.pipeline.pools.run_qualification", return_value=PROFILE_URL),
            patch("openoutreach.core.pipeline.pools.discover", return_value=100) as mock_discover,
        ):
            assert _advance("session", _qualifier("explore (BALD)")) is True
        mock_discover.assert_called_once()

    def test_empty_pool_and_dry_discovery_stalls(self):
        """No lead to label and nothing to discover → the engine has nothing to do."""
        with _engine([], qualify=None, discovered=0) as (_, mock_discover):
            assert _advance("session", _qualifier("explore (BALD)")) is False
        mock_discover.assert_called_once()

    def test_covered_pool_widens_before_labelling(self):
        """A deep but stale pool — every lead a near-duplicate of one already labelled —
        widens first, then labels from the enlarged pool."""
        stale = [Mock(embedding_array=np.zeros(384))]
        fresh = [Mock(embedding_array=np.ones(384))]
        with (
            patch("openoutreach.core.pipeline.pools.fetch_qualification_candidates",
                  side_effect=[stale, fresh]),
            patch("openoutreach.core.pipeline.pools.pool_is_covered", return_value=True),
            patch("openoutreach.core.pipeline.pools.run_qualification",
                  return_value=PROFILE_URL) as mock_qualify,
            patch("openoutreach.core.pipeline.pools.discover", return_value=100) as mock_discover,
        ):
            assert _advance("session", _qualifier("explore (BALD)")) is True

        mock_discover.assert_called_once()
        assert mock_qualify.call_args.kwargs["candidates"] == fresh

    def test_covered_pool_still_labels_when_discovery_is_dry(self):
        """Widening is a preference, not a gate: a saturated or unavailable provider must
        leave the label happening rather than stall the engine."""
        stale = [Mock(embedding_array=np.zeros(384))]
        with _engine(stale, discovered=0, covered=True) as (mock_qualify, mock_discover):
            assert _advance("session", _qualifier("explore (BALD)")) is True

        mock_discover.assert_called_once()
        assert mock_qualify.call_args.kwargs["candidates"] == stale

    def test_exploit_ignores_coverage(self):
        """Coverage is an explore concern — exploit wants conversion, and a pool with no
        novelty left is exactly where its best lead lives."""
        strong = Mock(embedding_array=np.ones(384))
        with _engine([strong], covered=True) as (mock_qualify, mock_discover):
            with patch.dict("openoutreach.core.pipeline.pools.CAMPAIGN_CONFIG",
                            {"min_gp_confidence": 0.9}):
                assert _advance("session", _qualifier("exploit (p)", probs=[0.95])) is True

        mock_discover.assert_not_called()
        assert mock_qualify.call_args.kwargs["candidates"] == [strong]


class TestFindCandidate:
    def test_returns_a_ready_candidate_immediately(self):
        scorer = BayesianQualifier(seed=42)
        with (
            patch("openoutreach.core.pipeline.pools.find_ready_candidate", return_value=CANDIDATE),
            patch("openoutreach.core.pipeline.pools.promote_to_ready") as mock_promote,
        ):
            assert find_candidate("session", scorer) == CANDIDATE
        mock_promote.assert_not_called()

    def test_promotes_then_returns(self):
        scorer = BayesianQualifier(seed=42)
        with (
            patch("openoutreach.core.pipeline.pools.find_ready_candidate",
                  side_effect=[None, CANDIDATE]),
            patch("openoutreach.core.pipeline.pools.promote_to_ready", return_value=1),
        ):
            assert find_candidate("session", scorer) == CANDIDATE

    def test_advances_then_promotes_then_returns(self):
        """No ready lead, nothing to promote yet → advance one unit, which qualifies a
        lead the next promote pass then lifts to ready."""
        scorer = BayesianQualifier(seed=42)
        with (
            patch("openoutreach.core.pipeline.pools.find_ready_candidate",
                  side_effect=[None, CANDIDATE]),
            patch("openoutreach.core.pipeline.pools.promote_to_ready", side_effect=[0, 1]),
            patch("openoutreach.core.pipeline.pools._advance", return_value=True) as mock_advance,
        ):
            assert find_candidate("session", scorer) == CANDIDATE
        mock_advance.assert_called_once()

    def test_stalled_engine_returns_none(self):
        scorer = BayesianQualifier(seed=42)
        with (
            patch("openoutreach.core.pipeline.pools.find_ready_candidate", return_value=None),
            patch("openoutreach.core.pipeline.pools.promote_to_ready", return_value=0),
            patch("openoutreach.core.pipeline.pools._advance", return_value=False),
        ):
            assert find_candidate("session", scorer) is None
