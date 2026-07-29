# tests/test_anchors.py
"""Cold-phase anchors — the synthetic ideal profiles that let a GP fit before any real
lead has qualified.

The LLM call (``run_agent_sync``) and the embedder are stubbed, so these assert the
lifecycle rather than the model: generated once, persisted on the campaign, reloaded
without a second LLM call, and retired one at a time as real acceptances replace them.
"""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from openoutreach.core.ml.qualifier import BayesianQualifier
from openoutreach.core.pipeline.icp import _AnchorProfiles, ensure_anchors, generate_anchors

pytestmark = pytest.mark.django_db


def _campaign(**kw):
    from openoutreach.core.models import Campaign

    defaults = dict(name="C", product_docs="p", campaign_target="t")
    defaults.update(kw)
    return Campaign.objects.create(**defaults)


@contextmanager
def _llm_returns(profiles):
    """Stub the whole LLM boundary — model resolution, Agent, and the run."""
    with (
        patch("openoutreach.core.llm.run_agent_sync",
              return_value=MagicMock(output=_AnchorProfiles(profiles=profiles))),
        patch("openoutreach.core.llm.get_llm_model"),
        patch("pydantic_ai.Agent"),
    ):
        yield


def _stub_embed():
    return patch("openoutreach.discovery.embed_profile",
                 side_effect=lambda text, *a, **kw: np.full(384, len(text), dtype=np.float32))


class TestGenerateAnchors:
    def test_normalizes_the_llm_output(self):
        with _llm_returns(["  Head Of Sales ACME  ", "", "cto northwind"]):
            assert generate_anchors(_campaign()) == ["head of sales acme", "cto northwind"]

    def test_an_llm_outage_leaves_the_campaign_unanchored(self):
        """Best-effort: an unanchored campaign still runs, just without a fitted GP."""
        with (
            patch("openoutreach.core.llm.run_agent_sync", side_effect=RuntimeError("down")),
            patch("openoutreach.core.llm.get_llm_model"),
            patch("pydantic_ai.Agent"),
        ):
            assert generate_anchors(_campaign()) == []


class TestEnsureAnchors:
    def test_generates_persists_and_embeds(self):
        campaign = _campaign()
        with _llm_returns(["cmo acme", "cto northwind"]), _stub_embed():
            embeddings = ensure_anchors(campaign)

        assert embeddings.shape == (2, 384)
        campaign.refresh_from_db()
        assert campaign.anchor_profiles == ["cmo acme", "cto northwind"]
        assert campaign.anchor_embeddings

    def test_reuses_the_stored_set_without_a_second_llm_call(self):
        """Re-inventing them each boot would re-anchor the GP somewhere slightly else."""
        campaign = _campaign()
        with _llm_returns(["cmo acme", "cto northwind"]), _stub_embed():
            first = ensure_anchors(campaign)

        with patch("openoutreach.core.llm.run_agent_sync",
                   side_effect=AssertionError("must not regenerate")):
            second = ensure_anchors(campaign)

        assert np.array_equal(first, second)

    def test_returns_none_without_icp_text(self):
        with patch("openoutreach.core.llm.run_agent_sync",
                   side_effect=AssertionError("nothing to generate from")):
            assert ensure_anchors(_campaign(product_docs="", campaign_target="")) is None

    def test_returns_none_when_the_llm_proposes_nothing(self):
        with _llm_returns([]):
            assert ensure_anchors(_campaign()) is None


class TestAnchorFillUp:
    """A short first round is filled up to ``ANCHOR_COUNT`` on the next call."""

    def test_fills_up_to_anchor_count(self):
        campaign = _campaign()
        with _llm_returns(["a one", "b two"]), _stub_embed():
            ensure_anchors(campaign)
        with _llm_returns(["c three"]), _stub_embed():
            embeddings = ensure_anchors(campaign)

        assert embeddings.shape == (3, 384)
        campaign.refresh_from_db()
        assert campaign.anchor_profiles == ["a one", "b two", "c three"]

    def test_asks_only_for_the_shortfall_and_shows_what_exists(self):
        """A second round must widen the ideal region, not restate it."""
        campaign = _campaign()
        with _llm_returns(["a one", "b two"]), _stub_embed():
            ensure_anchors(campaign)

        with (
            patch("openoutreach.core.pipeline.icp.generate_anchors",
                  return_value=["c three"]) as gen,
            _stub_embed(),
        ):
            ensure_anchors(campaign)

        assert gen.call_args.kwargs == {"count": 1, "existing": ["a one", "b two"]}

    def test_drops_profiles_the_model_repeated(self):
        campaign = _campaign()
        with _llm_returns(["a one"]), _stub_embed():
            ensure_anchors(campaign)
        with _llm_returns(["a one", "b two"]), _stub_embed():
            ensure_anchors(campaign)

        campaign.refresh_from_db()
        assert campaign.anchor_profiles == ["a one", "b two"]

    def test_a_failed_fill_up_keeps_what_is_already_there(self):
        campaign = _campaign()
        with _llm_returns(["a one"]), _stub_embed():
            first = ensure_anchors(campaign)

        with _llm_returns([]), _stub_embed():
            still = ensure_anchors(campaign)

        assert np.array_equal(first, still)

    def test_no_call_when_the_set_is_already_full(self):
        campaign = _campaign()
        with _llm_returns(["a one", "b two", "c three"]), _stub_embed():
            ensure_anchors(campaign)

        with patch("openoutreach.core.pipeline.icp.generate_anchors",
                   side_effect=AssertionError("already full")):
            assert ensure_anchors(campaign).shape == (3, 384)


def _rejections(qualifier, n):
    rng = np.random.RandomState(3)
    for _ in range(n):
        qualifier.update(rng.randn(384).astype(np.float32), 0)


class TestAnchorLifecycle:
    """Retirement is one countdown: ``ANCHOR_COUNT - n_real_positives``, nothing else."""

    def _anchored(self, campaign, profiles, rejections=0):
        with _llm_returns(profiles), _stub_embed():
            anchors = ensure_anchors(campaign)
        qualifier = BayesianQualifier(seed=42, campaign=campaign)
        _rejections(qualifier, rejections)
        qualifier.set_anchors(anchors)
        return qualifier

    def test_a_real_positive_retires_one_stored_anchor(self):
        """The handover is one-for-one: ground truth displaces the guess a lead at a
        time, so the positive class never lurches from dozens to one."""
        campaign = _campaign()
        qualifier = self._anchored(
            campaign, ["cmo acme", "cto northwind", "vp sales bo"], rejections=3)

        qualifier.update(np.zeros(384, dtype=np.float32), 0)
        campaign.refresh_from_db()
        assert len(campaign.anchor_profiles) == 3  # a rejection retires nothing

        qualifier.update(np.ones(384, dtype=np.float32), 1)

        campaign.refresh_from_db()
        # newest first — the campaign's opening statement of its ICP goes last
        assert campaign.anchor_profiles == ["cmo acme", "cto northwind"]
        assert qualifier.n_anchors == 2
        assert qualifier.is_cold is True

    def test_an_acceptance_before_any_rejection_retires_only_one(self):
        """The live regression: the budget used to be ``n_neg - n_real_pos``, which is 0
        on a campaign whose first verdict is an acceptance — so the first good lead
        dropped every anchor at once and left a positive class of exactly one."""
        campaign = _campaign()
        qualifier = self._anchored(campaign, ["cmo acme", "cto northwind", "vp sales bo"])

        qualifier.update(np.ones(384, dtype=np.float32), 1)

        assert qualifier.n_anchors == 2
        assert qualifier.is_cold is True
        assert qualifier.class_counts == (0, 3)

    def test_the_padding_survives_a_pile_of_rejections(self):
        """Rejections are not the clock. A campaign 8 rejections deep with one real
        positive still fits on a positive class of 3, not of 1."""
        campaign = _campaign()
        qualifier = self._anchored(
            campaign, ["cmo acme", "cto northwind", "vp sales bo"], rejections=8)
        qualifier.update(np.ones(384, dtype=np.float32), 1)

        assert qualifier.n_anchors == 2
        assert qualifier.class_counts == (8, 3)

    def test_the_last_anchor_goes_when_positives_reach_anchor_count(self):
        campaign = _campaign()
        qualifier = self._anchored(
            campaign, ["cmo acme", "cto northwind", "vp sales bo"], rejections=2)

        for _ in range(3):
            qualifier.update(np.ones(384, dtype=np.float32), 1)

        campaign.refresh_from_db()
        assert campaign.anchor_profiles == []
        assert campaign.anchor_embeddings is None
        assert qualifier.is_cold is False
        assert qualifier.class_counts == (2, 3)

    def test_a_retired_anchor_cannot_be_restored_by_a_later_boot(self):
        """The countdown is re-applied on every ``set_anchors``, so a stale stored set can
        never resurrect an anchor a real positive displaced."""
        campaign = _campaign()
        qualifier = self._anchored(campaign, ["cmo acme", "cto northwind", "vp sales bo"])
        stale = np.ones((3, 384), dtype=np.float32)

        for _ in range(3):
            qualifier.update(np.ones(384, dtype=np.float32), 1)
        qualifier.set_anchors(stale)

        assert qualifier.n_anchors == 0
        assert qualifier.class_counts == (0, 3)
