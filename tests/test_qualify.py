# tests/test_qualify.py
"""Tests for run_qualification — the qualify leg of the lazy chain.

Post-pivot, qualify only promotes (label=1) or disqualifies (label=0/promote
failure). Enrichment / email-resolution moved to the find-email leg. Leads carry
their own ``profile_text`` + embedding from discovery — no live scrape."""
from __future__ import annotations

from unittest.mock import Mock, patch

import numpy as np
import pytest

from openoutreach.core.ml.qualifier import BayesianQualifier
from openoutreach.core.pipeline.qualify import (
    farthest_from_labelled,
    labelled_resolution,
    pool_is_covered,
    run_qualification,
)


def _make_lead(profile_url="https://www.linkedin.com/in/alice/", profile_text="engineer at acme",
               embedding=None, creation_date=None):
    from openoutreach.crm.models import Lead

    if embedding is None:
        embedding = np.ones(384, dtype=np.float32)
    fields = {"creation_date": creation_date} if creation_date else {}
    return Lead.objects.create(
        profile_url=profile_url,
        profile_text=profile_text,
        embedding=np.asarray(embedding, dtype=np.float32).tobytes(),
        **fields,
    )


def _axis(index: int, scale: float = 1.0) -> np.ndarray:
    """A 384-dim vector on a single axis — orthogonal, so distances are exact."""
    vec = np.zeros(384, dtype=np.float32)
    vec[index] = scale
    return vec


@pytest.mark.django_db
class TestRunQualification:
    def test_calls_llm_on_candidate_with_profile_text(self, fake_session):
        _make_lead()
        qualifier = BayesianQualifier(seed=42)

        with (
            patch("openoutreach.core.ml.qualifier.qualify_with_llm",
                  return_value=(1, "Good fit")) as mock_llm,
            patch("openoutreach.core.db.leads.promote_lead_to_deal"),
        ):
            result = run_qualification(fake_session, qualifier)

        mock_llm.assert_called_once()
        assert result == "https://www.linkedin.com/in/alice/"

    def test_skips_when_profile_text_empty(self, fake_session):
        _make_lead(profile_text="")
        qualifier = BayesianQualifier(seed=42)

        with (
            patch("openoutreach.core.ml.qualifier.qualify_with_llm") as mock_llm,
            patch("openoutreach.core.db.leads.promote_lead_to_deal") as mock_promote,
        ):
            result = run_qualification(fake_session, qualifier)

        assert result is None
        mock_llm.assert_not_called()
        mock_promote.assert_not_called()

    def test_promotes_on_label_1(self, fake_session):
        _make_lead()
        qualifier = BayesianQualifier(seed=42)

        with (
            patch("openoutreach.core.ml.qualifier.qualify_with_llm", return_value=(1, "Good fit")),
            patch("openoutreach.core.db.leads.promote_lead_to_deal") as mock_promote,
            patch("openoutreach.core.db.deals.create_disqualified_deal") as mock_disq,
        ):
            run_qualification(fake_session, qualifier)

        mock_promote.assert_called_once()
        mock_disq.assert_not_called()

    def test_disqualifies_on_label_0(self, fake_session):
        _make_lead()
        qualifier = BayesianQualifier(seed=42)

        with (
            patch("openoutreach.core.ml.qualifier.qualify_with_llm", return_value=(0, "Bad fit")),
            patch("openoutreach.core.db.leads.promote_lead_to_deal") as mock_promote,
            patch("openoutreach.core.db.deals.create_disqualified_deal") as mock_disq,
        ):
            run_qualification(fake_session, qualifier)

        mock_promote.assert_not_called()
        mock_disq.assert_called_once()

    def test_disqualifies_when_promote_raises_value_error(self, fake_session):
        _make_lead()
        qualifier = BayesianQualifier(seed=42)

        with (
            patch("openoutreach.core.ml.qualifier.qualify_with_llm", return_value=(1, "Good fit")),
            patch("openoutreach.core.db.leads.promote_lead_to_deal",
                  side_effect=ValueError("no company_name")),
            patch("openoutreach.core.db.deals.create_disqualified_deal") as mock_disq,
        ):
            run_qualification(fake_session, qualifier)

        mock_disq.assert_called_once()

    def test_returns_none_when_no_candidates(self, fake_session):
        qualifier = BayesianQualifier(seed=42)

        with patch("openoutreach.core.ml.qualifier.qualify_with_llm") as mock_llm:
            assert run_qualification(fake_session, qualifier) is None

        mock_llm.assert_not_called()


@pytest.mark.django_db
class TestColdStartSelection:
    """Selection while the GP cannot fit — every label one class, so no posterior.

    This is the first-run state: the LLM rejects everything until the seed ICP is
    right, so ``acquisition_scores`` stays None and the pick has to come from the
    labelled points alone.
    """

    def test_picks_farthest_from_labelled_lead(self, fake_session):
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        # Oldest first (the order the pre-diversity fallback would have taken), and
        # deliberately the lead nearest the labelled point.
        _make_lead("https://www.linkedin.com/in/near/", "near", _axis(0),
                   creation_date=now - timedelta(hours=2))
        _make_lead("https://www.linkedin.com/in/mid/", "mid", _axis(1),
                   creation_date=now - timedelta(hours=1))
        _make_lead("https://www.linkedin.com/in/far/", "far", _axis(2, scale=5.0),
                   creation_date=now)

        qualifier = BayesianQualifier(seed=42)
        # Two rejections, one class — the GP stays unfitted, exactly the cold state.
        qualifier.update(_axis(0), 0)
        qualifier.update(_axis(0), 0)
        assert qualifier.acquisition_mode() is None

        with (
            patch("openoutreach.core.ml.qualifier.qualify_with_llm",
                  return_value=(0, "Bad fit")),
            patch("openoutreach.core.db.deals.create_disqualified_deal"),
        ):
            result = run_qualification(fake_session, qualifier)

        assert result == "https://www.linkedin.com/in/far/"

    def test_falls_back_to_oldest_when_nothing_labelled_yet(self, fake_session):
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        _make_lead("https://www.linkedin.com/in/first/", "first", _axis(0),
                   creation_date=now - timedelta(hours=1))
        _make_lead("https://www.linkedin.com/in/second/", "second", _axis(1),
                   creation_date=now)

        qualifier = BayesianQualifier(seed=42)

        with (
            patch("openoutreach.core.ml.qualifier.qualify_with_llm",
                  return_value=(0, "Bad fit")),
            patch("openoutreach.core.db.deals.create_disqualified_deal"),
        ):
            result = run_qualification(fake_session, qualifier)

        assert result == "https://www.linkedin.com/in/first/"


class TestFarthestFromLabelled:
    def test_scores_by_nearest_labelled_point(self):
        labelled = np.array([_axis(0), _axis(2, scale=5.0)], dtype=np.float64)
        candidates = np.array([_axis(0), _axis(1), _axis(2, scale=4.0)], dtype=np.float64)

        index, distance = farthest_from_labelled(labelled, candidates)

        # axis-1 is sqrt(2) from axis-0; axis-2@4.0 is only 1.0 from axis-2@5.0, so
        # the nearest-labelled score — not the farthest-labelled one — decides.
        assert index == 1
        assert distance == pytest.approx(np.sqrt(2.0))

    def test_resolution_is_the_mean_nearest_neighbour_distance(self):
        # Three points on one axis at 0, 1, 4: nearest-neighbour distances are 1, 1, 3.
        labelled = np.array([_axis(0, 0.0), _axis(0, 1.0), _axis(0, 4.0)], dtype=np.float64)

        assert labelled_resolution(labelled) == pytest.approx(5.0 / 3.0)

    def test_resolution_is_none_under_two_labels(self):
        assert labelled_resolution(np.empty((0, 384))) is None
        assert labelled_resolution(np.array([_axis(0)], dtype=np.float64)) is None

    def test_handles_a_candidate_identical_to_a_labelled_point(self):
        """Coincident points give distance 0 — the subtraction must not go negative."""
        labelled = np.array([_axis(0)], dtype=np.float64)
        candidates = np.array([_axis(0)], dtype=np.float64)

        index, distance = farthest_from_labelled(labelled, candidates)

        assert index == 0
        assert distance == pytest.approx(0.0)


class TestPoolIsCovered:
    """The explore branch's widen-vs-label signal, on its own geometry."""

    @staticmethod
    def _qualifier(labelled):
        qualifier = Mock()
        qualifier.labelled_embeddings = np.asarray(labelled, dtype=np.float64)
        return qualifier

    @staticmethod
    def _pool(*embeddings):
        return [Mock(embedding_array=e) for e in embeddings]

    def test_covered_when_the_best_candidate_is_nearer_than_the_labelled_spacing(self):
        # Labels 1.0 apart; the only candidate sits 0.1 from one of them.
        qualifier = self._qualifier([_axis(0, 0.0), _axis(0, 1.0)])
        pool = self._pool(_axis(0, 0.1))

        assert pool_is_covered(qualifier, pool) is True

    def test_not_covered_when_a_candidate_opens_new_ground(self):
        qualifier = self._qualifier([_axis(0, 0.0), _axis(0, 1.0)])
        pool = self._pool(_axis(0, 0.1), _axis(1, 9.0))

        assert pool_is_covered(qualifier, pool) is False

    def test_not_covered_under_two_labels(self):
        """No spacing to measure yet — never widen on an unmeasurable comparison."""
        qualifier = self._qualifier([_axis(0)])

        assert pool_is_covered(qualifier, self._pool(_axis(0))) is False

    def test_not_covered_when_the_pool_is_empty(self):
        """The empty-pool path is the caller's; this must not claim coverage for it."""
        qualifier = self._qualifier([_axis(0, 0.0), _axis(0, 1.0)])

        assert pool_is_covered(qualifier, []) is False

    def test_ratio_scales_the_bar(self):
        qualifier = self._qualifier([_axis(0, 0.0), _axis(0, 1.0)])
        pool = self._pool(_axis(0, 0.5))  # 0.5 away, labels spaced 1.0 apart

        with patch.dict("openoutreach.core.pipeline.qualify.CAMPAIGN_CONFIG",
                        {"novelty_ratio": 0.25}):
            assert pool_is_covered(qualifier, pool) is False
        with patch.dict("openoutreach.core.pipeline.qualify.CAMPAIGN_CONFIG",
                        {"novelty_ratio": 2.0}):
            assert pool_is_covered(qualifier, pool) is True
