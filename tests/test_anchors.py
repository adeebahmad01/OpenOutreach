# tests/test_anchors.py
"""Cold-phase anchors — the synthetic ideal profiles that let a GP fit before any real
lead has qualified.

The LLM call (``run_agent_sync``) and the embedder are stubbed, so these assert the
lifecycle rather than the model: generated once, persisted on the campaign, reloaded
without a second LLM call, and cleared the moment a real lead qualifies.
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


class TestRebalanceAnchors:
    """``pools._rebalance_anchors`` — the hook that keeps the classes level while cold."""

    def test_tops_up_when_rejections_outnumber_the_anchors(self):
        from openoutreach.core.pipeline.icp import ANCHOR_COUNT
        from openoutreach.core.pipeline.pools import _rebalance_anchors

        qualifier = BayesianQualifier(seed=42)
        session = MagicMock(campaign=_campaign())
        with (
            patch("openoutreach.core.pipeline.icp.ensure_anchors") as ensure,
            patch.object(BayesianQualifier, "class_counts", property(lambda self: (10, 3))),
        ):
            _rebalance_anchors(session, qualifier)

        assert ensure.call_args.kwargs["minimum"] == 10 + ANCHOR_COUNT

    def test_feeds_the_grown_set_back_into_the_qualifier(self):
        from openoutreach.core.pipeline.pools import _rebalance_anchors

        qualifier = BayesianQualifier(seed=42)
        session = MagicMock(campaign=_campaign())
        grown = np.ones((7, 384), dtype=np.float32)
        with (
            patch("openoutreach.core.pipeline.icp.ensure_anchors", return_value=grown),
            patch.object(BayesianQualifier, "class_counts", property(lambda self: (10, 3))),
        ):
            _rebalance_anchors(session, qualifier)

        assert len(qualifier._anchor_X) == 7

    def test_no_top_up_while_the_classes_are_level(self):
        from openoutreach.core.pipeline.pools import _rebalance_anchors

        qualifier = BayesianQualifier(seed=42)
        session = MagicMock(campaign=_campaign())
        with (
            patch("openoutreach.core.pipeline.icp.ensure_anchors",
                  side_effect=AssertionError("already balanced")),
            patch.object(BayesianQualifier, "class_counts", property(lambda self: (3, 3))),
        ):
            _rebalance_anchors(session, qualifier)


class TestAnchorLifecycle:
    def test_a_real_positive_clears_the_stored_anchors(self):
        """Point of the phase: real ground truth supersedes the guess, and a campaign
        carrying anchors is exactly one still waiting for its first positive."""
        campaign = _campaign()
        with _llm_returns(["cmo acme"]), _stub_embed():
            anchors = ensure_anchors(campaign)

        qualifier = BayesianQualifier(seed=42, campaign=campaign)
        qualifier.set_anchors(anchors)
        qualifier.update(np.zeros(384, dtype=np.float32), 0)
        campaign.refresh_from_db()
        assert campaign.anchor_profiles == ["cmo acme"]  # a rejection changes nothing

        qualifier.update(np.ones(384, dtype=np.float32), 1)

        campaign.refresh_from_db()
        assert campaign.anchor_profiles == []
        assert campaign.anchor_embeddings is None
        assert qualifier.has_real_positive is True

    def test_a_cleared_campaign_is_not_re_anchored(self):
        """The cold phase ends once, not once per boot."""
        campaign = _campaign()
        with _llm_returns(["cmo acme"]), _stub_embed():
            anchors = ensure_anchors(campaign)

        qualifier = BayesianQualifier(seed=42, campaign=campaign)
        qualifier.set_anchors(anchors)
        qualifier.update(np.ones(384, dtype=np.float32), 1)

        qualifier.set_anchors(anchors)  # a later boot re-offering them

        assert qualifier.class_counts == (0, 1)
