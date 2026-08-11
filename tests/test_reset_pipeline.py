# tests/test_reset_pipeline.py
"""`manage.py reset_pipeline` — start a campaign's walk over without losing its config."""
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from openoutreach.chat.models import ChatMessage
from openoutreach.core.models import Campaign, Keyword, QueryNode, SiteConfig
from openoutreach.core.pipeline.select import token_key
from openoutreach.crm.models import Deal, DealState, Lead


def _campaign(name="C", **kw):
    defaults = dict(product_docs="p", campaign_target="t", anchor_profiles=["ideal lead"],
                    model_blob=b"fitted", country_code="us")
    defaults.update(kw)
    return Campaign.objects.create(name=name, **defaults)


def _populate(campaign):
    pairs = [("lead_job_title", "founder")]
    node = QueryNode.objects.create(campaign=campaign, token_key=token_key(pairs))
    node.keywords.set(Keyword.rows_for(pairs))
    lead = Lead.objects.create(
        profile_url=f"https://x/{Lead.objects.count()}", profile_text="founder ai")
    deal = Deal.objects.create(lead=lead, campaign=campaign, state=DealState.QUALIFIED)
    ChatMessage.objects.create(deal=deal, content="hi")
    return node, lead, deal


def _run(**kw):
    out = StringIO()
    call_command("reset_pipeline", stdout=out, **kw)
    return out.getvalue()


class TestDiscoveryScope:
    def test_drops_the_walk_and_keeps_the_evidence(self, db):
        # The default scope: after changing the seed prompt you want a fresh vocabulary,
        # but the verdicts are what the new walk scores itself against.
        c = _campaign()
        _populate(c)

        _run(yes=True)

        assert QueryNode.objects.count() == 0
        assert Keyword.objects.count() == 0
        assert Lead.objects.count() == 1
        assert Deal.objects.count() == 1
        c.refresh_from_db()
        assert c.anchor_profiles == ["ideal lead"]
        assert c.model_blob == b"fitted"

    def test_leaves_configuration_alone(self, db):
        SiteConfig.load()
        c = _campaign()
        _populate(c)

        _run(yes=True, all=True)

        assert Campaign.objects.count() == 1
        assert SiteConfig.objects.count() == 1
        c.refresh_from_db()
        assert (c.product_docs, c.campaign_target, c.country_code) == ("p", "t", "us")


class TestFullScope:
    def test_deletes_the_leads_verdicts_and_derived_state(self, db):
        c = _campaign()
        _populate(c)

        _run(yes=True, all=True, no_backup=True)

        assert (QueryNode.objects.count(), Keyword.objects.count()) == (0, 0)
        assert (Lead.objects.count(), Deal.objects.count()) == (0, 0)
        assert ChatMessage.objects.count() == 0
        c.refresh_from_db()
        assert c.anchor_profiles == []
        assert c.anchor_embeddings is None
        assert c.model_blob is None


class TestScoping:
    def test_one_campaign_does_not_strip_anothers_vocabulary(self, db):
        # Keywords and Leads are global rows shared across campaigns — a partial reset
        # must not delete what another campaign is still working from.
        a, b = _campaign("A"), _campaign("B")
        _populate(a)
        _populate(b)

        _run(campaign="A", yes=True, all=True, no_backup=True)

        assert QueryNode.objects.filter(campaign=a).count() == 0
        assert QueryNode.objects.filter(campaign=b).count() == 1
        assert Keyword.objects.count() == 1     # kept: B still uses it
        assert Lead.objects.count() == 2        # kept: leads are campaign-agnostic
        assert Deal.objects.filter(campaign=b).count() == 1

    def test_unknown_campaign_is_an_error(self, db):
        _campaign()
        with pytest.raises(CommandError, match="No campaign named"):
            _run(campaign="nope", yes=True)

    def test_nothing_to_reset_is_not_an_error(self, db):
        _campaign()
        assert "Nothing to reset" in _run(yes=True)
