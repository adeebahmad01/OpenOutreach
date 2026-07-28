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
