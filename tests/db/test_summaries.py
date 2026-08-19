"""Tests for core/db/summaries.py — the mem0-style fact-list boundary."""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart

from tests.factories import LeadFactory, DealFactory


def _structured_test_model(output: dict) -> TestModel:
    """TestModel that yields *output* as the structured output args."""
    return TestModel(custom_output_args=output)


def _text_function_model(text: str) -> FunctionModel:
    """FunctionModel that returns a fixed text response on every call."""
    def _respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=text)])

    return FunctionModel(_respond)


def _capturing_function_model(captured: dict, output: dict) -> FunctionModel:
    """FunctionModel that records the messages it receives, then yields *output*."""
    from pydantic_ai.messages import ToolCallPart

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["messages"] = messages
        captured["output_tools"] = info.output_tools
        tool_name = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=output)])

    return FunctionModel(_respond)


@pytest.fixture
def deal_with_lead(db, campaign):
    lead = LeadFactory(profile_url="https://www.linkedin.com/in/alice/")
    return DealFactory(lead=lead, campaign=campaign)


class TestExtractFacts:
    def test_empty_input_returns_empty_list(self, db):
        from openoutreach.core.db.summaries import extract_facts

        assert extract_facts("", seller_name="Diego") == []
        assert extract_facts("   \n  ", seller_name="Diego") == []

    def test_invokes_llm_with_structured_output(self, db):
        from openoutreach.core.db.summaries import extract_facts

        captured: dict = {}
        model = _capturing_function_model(
            captured, {"facts": ["Works at Acme.", "Based in Berlin."]},
        )
        with patch("openoutreach.core.llm.get_llm_model", return_value=model):
            facts = extract_facts(
                "Alice works at Acme. She lives in Berlin.",
                seller_name="Diego",
                context="Campaign objective: hire engineers",
            )

        assert facts == ["Works at Acme.", "Based in Berlin."]
        # The system prompt carries the vendored prompt + identity binding +
        # context; the user message carries the input text.
        rendered = "\n".join(
            part.content
            for msg in captured["messages"]
            for part in msg.parts
            if hasattr(part, "content") and isinstance(part.content, str)
        )
        assert "Campaign objective" in rendered
        assert "Alice works at Acme" in rendered
        assert "[Me] is named Diego" in rendered


class TestMaterializeProfileSummary:
    def test_noop_when_already_built(self, db, deal_with_lead):
        from openoutreach.core.db.summaries import materialize_profile_summary_if_missing

        deal_with_lead.profile_summary = {"facts": ["already built"]}
        deal_with_lead.save(update_fields=["profile_summary"])

        with patch("openoutreach.core.db.summaries.extract_facts") as mock_extract:
            materialize_profile_summary_if_missing(deal_with_lead)

        mock_extract.assert_not_called()

    def test_builds_from_profile_text_and_persists(self, db, campaign, deal_with_lead):
        from openoutreach.core.db.summaries import materialize_profile_summary_if_missing

        deal_with_lead.lead.profile_text = "senior engineer at acme"
        deal_with_lead.lead.save(update_fields=["profile_text"])

        with patch("openoutreach.core.db.summaries.extract_facts",
                   return_value=["Senior Engineer at Acme.", "URN ABC123."]) as mock_extract:
            materialize_profile_summary_if_missing(deal_with_lead)

        mock_extract.assert_called_once()
        # Facts are extracted from the stored profile_text — no re-scrape.
        assert mock_extract.call_args.args[0] == "senior engineer at acme"
        deal_with_lead.refresh_from_db()
        assert deal_with_lead.profile_summary == {
            "facts": ["Senior Engineer at Acme.", "URN ABC123."]
        }

    def test_no_profile_text_logs_and_skips(self, db, campaign, deal_with_lead, caplog):
        from openoutreach.core.db.summaries import materialize_profile_summary_if_missing

        deal_with_lead.lead.profile_text = ""
        deal_with_lead.lead.save(update_fields=["profile_text"])

        with patch("openoutreach.core.db.summaries.extract_facts") as mock_extract:
            materialize_profile_summary_if_missing(deal_with_lead)

        mock_extract.assert_not_called()
        deal_with_lead.refresh_from_db()
        assert deal_with_lead.profile_summary is None


# ``TestUpdateChatSummary`` stood here — five tests over the chat summary the outreach
# agent carried across a thread. The function and the ``Deal.chat_summary`` column both
# left with the sending leg, so there is no conversation to summarise.
#
# ``TestReconcileFacts`` below stays: reconciliation is still used by the profile
# summary, which is about the lead rather than about a thread with them.


class TestReconcileFacts:
    """reconcile_facts wraps mem0's UPDATE prompt — mock the LLM at the boundary."""

    BINDING = {"seller_name": "Diego"}

    def test_empty_new_facts_returns_existing_unchanged(self, db):
        from openoutreach.core.db.summaries import reconcile_facts

        with patch("openoutreach.core.llm.get_llm_model") as mock_factory:
            result = reconcile_facts(["fact a", "fact b"], [], **self.BINDING)

        assert result == ["fact a", "fact b"]
        mock_factory.assert_not_called()

    def test_contradiction_drops_stale_fact(self, db):
        """LLM returns DELETE for the stale fact + ADD for the new one — both applied."""
        from openoutreach.core.db.summaries import reconcile_facts

        actions = [
            {"id": "0", "text": "Lead has no budget.", "event": "DELETE"},
            {"id": "1", "text": "Lead has budget.", "event": "ADD"},
        ]
        model = _text_function_model(json.dumps({"memory": actions}))
        with patch("openoutreach.core.llm.get_llm_model", return_value=model):
            result = reconcile_facts(
                ["Lead has no budget."],
                ["Lead has budget."],
                **self.BINDING,
            )

        assert result == ["Lead has budget."]

    def test_update_event_replaces_in_place(self, db):
        from openoutreach.core.db.summaries import reconcile_facts

        actions = [
            {"id": "0", "text": "Lead is CTO at Acme.", "event": "UPDATE",
             "old_memory": "Lead is an engineer at Acme."},
        ]
        model = _text_function_model(json.dumps({"memory": actions}))
        with patch("openoutreach.core.llm.get_llm_model", return_value=model):
            result = reconcile_facts(
                ["Lead is an engineer at Acme."],
                ["Lead is CTO at Acme."],
                **self.BINDING,
            )

        assert result == ["Lead is CTO at Acme."]

    def test_unknown_id_in_update_is_skipped(self, db, caplog):
        """LLM hallucinates an id that doesn't exist — log + skip, don't crash."""
        from openoutreach.core.db.summaries import reconcile_facts

        actions = [
            {"id": "999", "text": "Hallucinated.", "event": "UPDATE"},
            {"id": "0", "text": "Real ADD.", "event": "ADD"},
        ]
        model = _text_function_model(json.dumps({"memory": actions}))
        with caplog.at_level("WARNING"), \
             patch("openoutreach.core.llm.get_llm_model", return_value=model):
            result = reconcile_facts(["existing fact"], ["new fact"], **self.BINDING)

        assert "existing fact" in result
        assert "Real ADD." in result
        assert "Hallucinated." not in result
        assert any("UPDATE skipped" in r.message for r in caplog.records)

    def test_none_event_is_noop(self, db):
        from openoutreach.core.db.summaries import reconcile_facts

        actions = [
            {"id": "0", "text": "Lead is the founder.", "event": "NONE"},
            {"id": "1", "text": "Lead replied politely.", "event": "ADD"},
        ]
        model = _text_function_model(json.dumps({"memory": actions}))
        with patch("openoutreach.core.llm.get_llm_model", return_value=model):
            result = reconcile_facts(
                ["Lead is the founder."],
                ["Lead replied politely."],
                **self.BINDING,
            )

        assert result == ["Lead is the founder.", "Lead replied politely."]

    def test_markdown_wrapped_json_is_parsed(self, db):
        """Provider that wraps JSON in ```json ... ``` should still parse via fallback."""
        from openoutreach.core.db.summaries import reconcile_facts

        wrapped = (
            "```json\n"
            '{"memory": [{"id": "0", "text": "Lead is in Berlin.", "event": "ADD"}]}\n'
            "```"
        )
        model = _text_function_model(wrapped)
        with patch("openoutreach.core.llm.get_llm_model", return_value=model):
            result = reconcile_facts([], ["Lead is in Berlin."], **self.BINDING)

        assert result == ["Lead is in Berlin."]

    def test_reasoning_model_think_block_is_stripped(self, db):
        """Reasoning model output with <think> blocks before the JSON parses cleanly."""
        from openoutreach.core.db.summaries import reconcile_facts

        wrapped = (
            "<think>The user wants me to add this fact about location.</think>\n"
            '{"memory": [{"id": "0", "text": "Lead is in Berlin.", "event": "ADD"}]}'
        )
        model = _text_function_model(wrapped)
        with patch("openoutreach.core.llm.get_llm_model", return_value=model):
            result = reconcile_facts([], ["Lead is in Berlin."], **self.BINDING)

        assert result == ["Lead is in Berlin."]
