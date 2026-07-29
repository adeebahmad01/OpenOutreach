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


class TestAnchorTopUp:
    """The synthetic positive class grows to keep pace with the rejections it faces."""

    def test_tops_up_to_the_requested_minimum(self):
        campaign = _campaign()
        with _llm_returns(["a one", "b two"]), _stub_embed():
            ensure_anchors(campaign, minimum=2)
        with _llm_returns(["c three", "d four"]), _stub_embed():
            embeddings = ensure_anchors(campaign, minimum=4)

        assert embeddings.shape == (4, 384)
        campaign.refresh_from_db()
        assert campaign.anchor_profiles == ["a one", "b two", "c three", "d four"]

    def test_asks_only_for_the_shortfall_and_shows_what_exists(self):
        """A top-up must widen the ideal region, not restate it."""
        campaign = _campaign()
        with _llm_returns(["a one"]), _stub_embed():
            ensure_anchors(campaign, minimum=1)

        with (
            patch("openoutreach.core.pipeline.icp.generate_anchors",
                  return_value=["b two", "c three"]) as gen,
            _stub_embed(),
        ):
            ensure_anchors(campaign, minimum=3)

        assert gen.call_args.kwargs == {"count": 2, "existing": ["a one"]}

    def test_drops_profiles_the_model_repeated(self):
        campaign = _campaign()
        with _llm_returns(["a one"]), _stub_embed():
            ensure_anchors(campaign, minimum=1)
        with _llm_returns(["a one", "b two"]), _stub_embed():
            ensure_anchors(campaign, minimum=3)

        campaign.refresh_from_db()
        assert campaign.anchor_profiles == ["a one", "b two"]

    def test_a_failed_top_up_keeps_what_is_already_there(self):
        campaign = _campaign()
        with _llm_returns(["a one"]), _stub_embed():
            first = ensure_anchors(campaign, minimum=1)

        with _llm_returns([]), _stub_embed():
            still = ensure_anchors(campaign, minimum=5)

        assert np.array_equal(first, still)

    def test_no_call_when_the_minimum_is_already_met(self):
        campaign = _campaign()
        with _llm_returns(["a one", "b two"]), _stub_embed():
            ensure_anchors(campaign, minimum=2)

        with patch("openoutreach.core.pipeline.icp.generate_anchors",
                   side_effect=AssertionError("already balanced")):
            assert ensure_anchors(campaign, minimum=2).shape == (2, 384)


def _rejections(qualifier, n):
    rng = np.random.RandomState(3)
    for _ in range(n):
        qualifier.update(rng.randn(384).astype(np.float32), 0)


class TestRebalanceAnchors:
    """``pools._rebalance_anchors`` — the growing half of the anchor budget."""

    def test_tops_up_to_the_shortfall_the_real_positives_leave(self):
        from openoutreach.core.pipeline.pools import _rebalance_anchors

        qualifier = BayesianQualifier(seed=42)
        _rejections(qualifier, 10)
        session = MagicMock(campaign=_campaign())
        with patch("openoutreach.core.pipeline.icp.ensure_anchors") as ensure:
            _rebalance_anchors(session, qualifier)

        assert ensure.call_args.kwargs["minimum"] == 10  # n_neg - n_real_pos, no headroom

    def test_the_shortfall_shrinks_as_real_positives_arrive(self):
        """Anchors pad what ground truth has not supplied — a real acceptance is one
        fewer invented profile the campaign is entitled to."""
        from openoutreach.core.pipeline.pools import _rebalance_anchors

        qualifier = BayesianQualifier(seed=42)
        _rejections(qualifier, 10)
        qualifier.update(np.ones(384, dtype=np.float32), 1)
        qualifier.update(np.ones(384, dtype=np.float32), 1)
        session = MagicMock(campaign=_campaign())
        with patch("openoutreach.core.pipeline.icp.ensure_anchors") as ensure:
            _rebalance_anchors(session, qualifier)

        assert ensure.call_args.kwargs["minimum"] == 8

    def test_feeds_the_grown_set_back_into_the_qualifier(self):
        from openoutreach.core.pipeline.pools import _rebalance_anchors

        qualifier = BayesianQualifier(seed=42)
        _rejections(qualifier, 10)
        session = MagicMock(campaign=_campaign())
        grown = np.ones((7, 384), dtype=np.float32)
        with patch("openoutreach.core.pipeline.icp.ensure_anchors", return_value=grown):
            _rebalance_anchors(session, qualifier)

        assert qualifier.n_anchors == 7

    def test_no_top_up_until_the_gap_is_a_full_batch_wide(self):
        """Rationing: one LLM call per ANCHOR_COUNT rejections, not one per rejection."""
        from openoutreach.core.pipeline.pools import _rebalance_anchors

        qualifier = BayesianQualifier(seed=42)
        _rejections(qualifier, 4)
        qualifier.set_anchors(np.ones((3, 384), dtype=np.float32))
        session = MagicMock(campaign=_campaign())
        with patch("openoutreach.core.pipeline.icp.ensure_anchors",
                   side_effect=AssertionError("gap is only 1 wide")):
            _rebalance_anchors(session, qualifier)


class TestAnchorLifecycle:
    def _anchored(self, campaign, profiles):
        with _llm_returns(profiles), _stub_embed():
            anchors = ensure_anchors(campaign, minimum=len(profiles))
        qualifier = BayesianQualifier(seed=42, campaign=campaign)
        _rejections(qualifier, len(profiles))
        qualifier.set_anchors(anchors)
        return qualifier

    def test_a_real_positive_retires_one_stored_anchor(self):
        """The handover is one-for-one: ground truth displaces the guess a lead at a
        time, so the positive class never lurches from dozens to one."""
        campaign = _campaign()
        qualifier = self._anchored(campaign, ["cmo acme", "cto northwind", "vp sales bo"])

        qualifier.update(np.zeros(384, dtype=np.float32), 0)
        campaign.refresh_from_db()
        assert len(campaign.anchor_profiles) == 3  # a rejection retires nothing

        qualifier.update(np.ones(384, dtype=np.float32), 1)

        campaign.refresh_from_db()
        # 4 rejections, 1 real positive -> a budget of 3, and the newest anchor goes first
        assert campaign.anchor_profiles == ["cmo acme", "cto northwind", "vp sales bo"]

        qualifier.update(np.ones(384, dtype=np.float32), 1)
        campaign.refresh_from_db()
        assert campaign.anchor_profiles == ["cmo acme", "cto northwind"]
        assert qualifier.n_anchors == 2
        assert qualifier.is_cold is True

    def test_the_last_anchor_goes_when_positives_reach_the_rejections(self):
        campaign = _campaign()
        qualifier = self._anchored(campaign, ["cmo acme", "cto northwind"])

        for _ in range(2):
            qualifier.update(np.ones(384, dtype=np.float32), 1)

        campaign.refresh_from_db()
        assert campaign.anchor_profiles == []
        assert campaign.anchor_embeddings is None
        assert qualifier.is_cold is False
        assert qualifier.class_counts == (2, 2)

    def test_a_retired_anchor_cannot_be_restored_by_a_later_boot(self):
        """The budget is re-applied on every ``set_anchors``, so a stale stored set (or a
        top-up racing a retirement) can never resurrect an anchor a positive displaced."""
        campaign = _campaign()
        qualifier = self._anchored(campaign, ["cmo acme", "cto northwind"])
        stale = np.ones((2, 384), dtype=np.float32)

        for _ in range(2):
            qualifier.update(np.ones(384, dtype=np.float32), 1)
        qualifier.set_anchors(stale)

        assert qualifier.n_anchors == 0
        assert qualifier.class_counts == (2, 2)
