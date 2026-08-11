# tests/test_qualify.py
"""Tests for run_qualification — the qualify leg of the lazy chain.

Post-pivot, qualify only promotes (label=1) or disqualifies (label=0/promote
failure). Enrichment / email-resolution moved to the find-email leg. Leads carry
their own ``profile_text`` + embedding from discovery — no live scrape."""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from openoutreach.core.ml.qualifier import BayesianQualifier
from openoutreach.core.pipeline.qualify import run_qualification


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
    def test_calls_llm_on_candidate_with_profile_text(self, campaign):
        _make_lead()
        qualifier = BayesianQualifier(seed=42)

        with (
            patch("openoutreach.core.ml.qualifier.qualify_with_llm",
                  return_value=(1, "Good fit")) as mock_llm,
            patch("openoutreach.core.db.leads.promote_lead_to_deal"),
        ):
            result = run_qualification(campaign, qualifier)

        mock_llm.assert_called_once()
        assert result == "https://www.linkedin.com/in/alice/"

    def test_skips_when_profile_text_empty(self, campaign):
        _make_lead(profile_text="")
        qualifier = BayesianQualifier(seed=42)

        with (
            patch("openoutreach.core.ml.qualifier.qualify_with_llm") as mock_llm,
            patch("openoutreach.core.db.leads.promote_lead_to_deal") as mock_promote,
        ):
            result = run_qualification(campaign, qualifier)

        assert result is None
        mock_llm.assert_not_called()
        mock_promote.assert_not_called()

    def test_promotes_on_label_1(self, campaign):
        _make_lead()
        qualifier = BayesianQualifier(seed=42)

        with (
            patch("openoutreach.core.ml.qualifier.qualify_with_llm", return_value=(1, "Good fit")),
            patch("openoutreach.core.db.leads.promote_lead_to_deal") as mock_promote,
            patch("openoutreach.core.db.deals.create_disqualified_deal") as mock_disq,
        ):
            run_qualification(campaign, qualifier)

        mock_promote.assert_called_once()
        mock_disq.assert_not_called()

    def test_disqualifies_on_label_0(self, campaign):
        _make_lead()
        qualifier = BayesianQualifier(seed=42)

        with (
            patch("openoutreach.core.ml.qualifier.qualify_with_llm", return_value=(0, "Bad fit")),
            patch("openoutreach.core.db.leads.promote_lead_to_deal") as mock_promote,
            patch("openoutreach.core.db.deals.create_disqualified_deal") as mock_disq,
        ):
            run_qualification(campaign, qualifier)

        mock_promote.assert_not_called()
        mock_disq.assert_called_once()

    def test_disqualifies_when_promote_raises_value_error(self, campaign):
        _make_lead()
        qualifier = BayesianQualifier(seed=42)

        with (
            patch("openoutreach.core.ml.qualifier.qualify_with_llm", return_value=(1, "Good fit")),
            patch("openoutreach.core.db.leads.promote_lead_to_deal",
                  side_effect=ValueError("no company_name")),
            patch("openoutreach.core.db.deals.create_disqualified_deal") as mock_disq,
        ):
            run_qualification(campaign, qualifier)

        mock_disq.assert_called_once()

    def test_returns_none_when_no_candidates(self, campaign):
        qualifier = BayesianQualifier(seed=42)

        with patch("openoutreach.core.ml.qualifier.qualify_with_llm") as mock_llm:
            assert run_qualification(campaign, qualifier) is None

        mock_llm.assert_not_called()


@pytest.mark.django_db
class TestUnanchoredSelection:
    """The degraded path: anchoring failed, so the label set is still single-class and
    no posterior exists to rank with. Oldest first — nothing here can rank."""

    def test_falls_back_to_oldest(self, campaign):
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
            result = run_qualification(campaign, qualifier)

        assert result == "https://www.linkedin.com/in/first/"
