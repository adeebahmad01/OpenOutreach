"""Tests for the outreach agent's context builder + unified Jinja template."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.factories import LeadFactory, DealFactory


@pytest.fixture
def deal_with_summaries(db, fake_session):
    lead = LeadFactory()
    return DealFactory(
        lead=lead,
        campaign=fake_session.campaign,
        email_message_id="<root@corp.com>",
        profile_summary={"facts": [
            "Senior engineer at Acme Corp.",
            "Based in Berlin, Germany.",
            "Speaks English and German.",
        ]},
        chat_summary={"facts": [
            "Lead is curious about pricing.",
            "Lead has a small team budget.",
        ]},
    )


def _msg(content, is_outgoing):
    m = MagicMock()
    m.content = content
    m.is_outgoing = is_outgoing
    m.creation_date = None
    return m


def _self_profile(session):
    """Stub session.self_profile so the prompt builder works without a browser."""
    session.self_profile = {"first_name": "Bob", "last_name": "Builder", "urn": "urn:li:fsd_profile:SELF"}


class TestRenderSystemPrompt:
    def test_in_thread_includes_three_summary_blocks(self, db, fake_session, deal_with_summaries):
        from openoutreach.core.agents.outreach import _render_system_prompt

        _self_profile(fake_session)

        recent = [_msg("Hi, what do you do?", is_outgoing=True), _msg("Sales tooling.", is_outgoing=False)]
        prompt = _render_system_prompt(fake_session, deal_with_summaries, recent, is_first_touch=False)

        # Profile facts appear under the lead-knowledge block.
        assert "Senior engineer at Acme Corp." in prompt
        assert "Based in Berlin, Germany." in prompt
        # Chat facts appear under the conversation-knowledge block.
        assert "Lead is curious about pricing." in prompt
        # Verbatim recent messages appear in Me:/Lead: format.
        assert "Me: Hi, what do you do?" in prompt
        assert "Lead: Sales tooling." in prompt
        # The legacy flat fields are gone.
        assert "Headline:" not in prompt
        assert "Company:" not in prompt

    def test_in_thread_offers_all_three_actions(self, db, fake_session, deal_with_summaries):
        from openoutreach.core.agents.outreach import _render_system_prompt

        _self_profile(fake_session)
        prompt = _render_system_prompt(fake_session, deal_with_summaries, [], is_first_touch=False)

        assert "**wait**" in prompt
        assert "**mark_completed**" in prompt
        assert "having an email conversation" in prompt

    def test_first_touch_drops_the_conversation_blocks(self, db, fake_session, deal_with_summaries):
        """No thread yet — no chat summary, no transcript, and no wait/complete choice."""
        from openoutreach.core.agents.outreach import _render_system_prompt

        _self_profile(fake_session)
        prompt = _render_system_prompt(fake_session, deal_with_summaries, [], is_first_touch=True)

        # Lead facts still there; conversation facts are not.
        assert "Senior engineer at Acme Corp." in prompt
        assert "Lead is curious about pricing." not in prompt
        assert "Conversation So Far" not in prompt
        # The opener is forced to send and to carry a subject.
        assert "you always send" in prompt
        assert "`subject`" in prompt
        assert "**mark_completed**" not in prompt

    def test_both_ends_carry_the_research_framing(self, db, fake_session, deal_with_summaries):
        from openoutreach.core.agents.outreach import _render_system_prompt

        _self_profile(fake_session)
        for first_touch in (True, False):
            prompt = _render_system_prompt(
                fake_session, deal_with_summaries, [], is_first_touch=first_touch,
            )
            assert "You follow the Mom Test method." in prompt
            # Discovery is the default mode; pitching only on an explicit pull.
            assert "### Discovery (default, and where you stay)" in prompt
            assert "### Pitching (only when the lead pulls you there)" in prompt
            assert "Never volunteer the product" in prompt

    def test_handles_missing_summaries_gracefully(self, db, fake_session):
        from openoutreach.core.agents.outreach import _render_system_prompt

        lead = LeadFactory()
        deal = DealFactory(lead=lead, campaign=fake_session.campaign, email_message_id="<root@corp.com>")
        _self_profile(fake_session)

        prompt = _render_system_prompt(fake_session, deal, [], is_first_touch=False)

        # Renders without crashing and shows the empty placeholders.
        assert "(none yet)" in prompt
        assert "No recent messages." in prompt


class TestValidateOpener:
    def test_rejects_an_opener_without_a_subject(self):
        from openoutreach.core.agents.outreach import OutreachDecision, _validate_opener

        decision = OutreachDecision(action="send_message", message="Hi.", follow_up_hours=48)
        with pytest.raises(ValueError, match="no subject"):
            _validate_opener(decision, "lead@corp.com")

    def test_rejects_an_opener_that_does_not_send(self):
        from openoutreach.core.agents.outreach import OutreachDecision, _validate_opener

        decision = OutreachDecision(action="wait", follow_up_hours=48)
        with pytest.raises(ValueError, match="must send_message"):
            _validate_opener(decision, "lead@corp.com")

    def test_accepts_a_well_formed_opener(self):
        from openoutreach.core.agents.outreach import OutreachDecision, _validate_opener

        decision = OutreachDecision(
            action="send_message", subject="quick question", message="Hi.", follow_up_hours=48,
        )
        _validate_opener(decision, "lead@corp.com")


class TestLoadRecentMessages:
    def test_returns_last_n_in_chronological_order(self, db, fake_session):
        from openoutreach.chat.models import ChatMessage
        from django.utils import timezone
        from datetime import timedelta

        from openoutreach.core.agents.outreach import _load_recent_messages, RECENT_MESSAGES_WINDOW

        lead = LeadFactory()
        deal = DealFactory(lead=lead, campaign=fake_session.campaign)

        base = timezone.now()
        for i in range(RECENT_MESSAGES_WINDOW + 3):
            ChatMessage.objects.create(
                deal=deal,
                content=f"msg-{i}",
                is_outgoing=(i % 2 == 0),
                owner=fake_session.django_user,
                external_id=f"urn:msg:{i}",
                creation_date=base + timedelta(minutes=i),
            )

        recent = _load_recent_messages(deal)

        # Window respected and chronological order preserved.
        assert len(recent) == RECENT_MESSAGES_WINDOW
        contents = [m.content for m in recent]
        assert contents == sorted(contents, key=lambda c: int(c.split("-")[1]))
        # Returned the *latest* messages.
        assert contents[-1] == f"msg-{RECENT_MESSAGES_WINDOW + 2}"
