# openoutreach/core/agents/outreach.py
"""The outreach agent: one prompt, one decision type, both ends of the thread.

There is a single conversational agent. The cold open and every later reply are
the same voice doing the same job — Mom Test research, not selling — so they
render from one template (``outreach_agent.j2``) which branches on the only thing
that actually differs: whether a thread exists yet.

- **first touch** (no thread): the agent must ``send_message`` and must supply a
  ``subject``. ``emails/tasks/send.py`` sends it and records the thread root.
- **in thread**: replies are read from the mailbox first, then the agent picks
  ``send_message`` / ``wait`` / ``mark_completed``. ``emails/tasks/follow_up.py``
  executes the choice.

Single LLM call with structured output — no tool-calling loop.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from django.utils import timezone
from pydantic import BaseModel, Field, model_validator
from pydantic_ai import Agent

from openoutreach.core.agents.prompt import _format_facts, base_context, render
from openoutreach.core.llm import get_llm_model, run_agent_sync

logger = logging.getLogger(__name__)


class OutreachDecision(BaseModel):
    """Structured output from the outreach agent, at either end of the thread."""

    action: Literal["send_message", "mark_completed", "wait"] = Field(
        description="What to do next for this lead. The first email in a thread is always send_message.",
    )
    subject: str | None = Field(
        default=None,
        description=(
            "Subject line. Required on the first email of a thread — short and specific, "
            "like a real person wrote it, not salesy. Omit when replying in an existing thread."
        ),
    )
    message: str | None = Field(
        default=None,
        description="The email to send. Required when action='send_message'. A few short sentences, no signature.",
    )
    outcome: Literal[
        "converted", "not_interested", "wrong_fit", "no_budget",
        "has_solution", "bad_timing", "unresponsive",
    ] | None = Field(
        default=None,
        description="Why the conversation ended. Required when action='mark_completed'.",
    )
    follow_up_hours: float = Field(
        description="Hours until the next follow-up. Always required — you decide the pace.",
    )

    @model_validator(mode="after")
    def _check_required_fields(self):
        if self.action == "send_message" and not self.message:
            raise ValueError("message is required when action='send_message'")
        if self.action == "mark_completed" and not self.outcome:
            raise ValueError("outcome is required when action='mark_completed'")
        return self


# Number of trailing verbatim messages the agent sees alongside the rolling
# chat_summary. Older turns live in the summary fact list; the recency window
# preserves literal phrasing for the turns that matter most when composing
# the next reply.
RECENT_MESSAGES_WINDOW = 6


def run_outreach_agent(session, deal) -> OutreachDecision:
    """Decide the next move for ``deal`` — the cold open or the next turn in its thread.

    On an existing thread, replies are synced from the mailbox first (folding new
    inbound messages into ``deal.chat_summary``) so the agent decides on a current
    view. On a first touch there is nothing to read, and the decision is validated
    to be a sendable opener with a subject.
    """
    public_id = deal.lead.profile_url
    is_first_touch = not deal.email_message_id

    if not is_first_touch:
        from openoutreach.emails.inbox import sync_inbox

        sync_inbox(session, deal)
        deal.refresh_from_db(fields=["chat_summary", "profile_summary"])
        _log_chat_facts(public_id, deal)

    recent = [] if is_first_touch else _load_recent_messages(deal)
    system_prompt = _render_system_prompt(session, deal, recent, is_first_touch)

    agent = Agent(
        get_llm_model(),
        output_type=OutreachDecision,
        model_settings={"temperature": 0.7, "timeout": 60},
    )
    decision = run_agent_sync(agent.run(system_prompt)).output
    if decision is None:
        raise RuntimeError(f"LLM returned unparseable response for outreach to {public_id}")
    if is_first_touch:
        _validate_opener(decision, public_id)

    logger.info("outreach agent for %s: %s", public_id, decision.action)
    return decision


def _validate_opener(decision: OutreachDecision, public_id: str) -> None:
    """A first touch must be a sendable email with its own subject."""
    if decision.action != "send_message":
        raise ValueError(f"opener for {public_id} must send_message, got {decision.action}")
    if not decision.subject:
        raise ValueError(f"opener for {public_id} has no subject")


# ── Prompt context ────────────────────────────────────────────────


def _render_system_prompt(session, deal, recent_messages: list, is_first_touch: bool) -> str:
    """Render the outreach prompt for whichever end of the thread we're at."""
    now = timezone.now()
    thread_context = {} if is_first_touch else {
        "chat_summary": _format_facts(deal.chat_summary),
        "recent_messages": _format_recent_messages(recent_messages, now),
        "today": now.strftime("%Y-%m-%d"),
        "days_since_last_outgoing": _days_since_last_outgoing(recent_messages, now),
        "unanswered_outgoing": _count_unanswered_outgoing(recent_messages),
    }
    return render(
        "outreach_agent.j2",
        **base_context(session, deal),
        is_first_touch=is_first_touch,
        **thread_context,
    )


def _load_recent_messages(deal, limit: int = RECENT_MESSAGES_WINDOW) -> list:
    """Last `limit` ChatMessages for `deal`, in chronological order.

    The recency window of verbatim turns the agent sees alongside the rolling
    ``chat_summary`` — the opener plus any replies read from the mailbox.
    """
    from openoutreach.chat.models import ChatMessage

    qs = ChatMessage.objects.filter(deal=deal).order_by("-creation_date", "-pk")[:limit]
    return list(reversed(list(qs)))


def _format_recent_messages(messages: list, now: datetime) -> str:
    """Render the last few ChatMessage rows as a timestamped transcript."""
    if not messages:
        return "No recent messages."
    lines = []
    for m in messages:
        content = (m.content or "").strip()
        if not content:
            continue
        speaker = "Me" if m.is_outgoing else "Lead"
        prefix = f"{speaker} ({_humanize_age(m.creation_date, now)})" if m.creation_date else speaker
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines) or "No recent messages."


def _humanize_age(when: datetime, now: datetime) -> str:
    """Render `when` as a coarse age relative to `now` (e.g. ``3d ago``)."""
    delta = now - when
    if delta < timedelta(hours=1):
        return f"{max(int(delta.total_seconds() // 60), 1)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


def _days_since_last_outgoing(messages: list, now: datetime) -> int | None:
    """Whole days since the most recent outgoing message, or None if there are none."""
    timestamps = [m.creation_date for m in messages if m.is_outgoing and m.creation_date]
    if not timestamps:
        return None
    return max((now - max(timestamps)).days, 0)


def _count_unanswered_outgoing(messages: list) -> int:
    """Trailing run of outgoing messages with no lead reply after them."""
    count = 0
    for m in reversed(messages):
        if m.is_outgoing:
            count += 1
        else:
            break
    return count


def _log_chat_facts(public_id: str, deal) -> None:
    """Log the mem0 chat facts the agent is working with."""
    chat_facts = (deal.chat_summary or {}).get("facts", [])
    if not chat_facts:
        return
    lines = [f"chat facts for {public_id}:"]
    lines.extend(f"  • {f}" for f in chat_facts)
    logger.info("\n".join(lines))
