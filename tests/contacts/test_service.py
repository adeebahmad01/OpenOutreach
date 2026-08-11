# tests/contacts/test_service.py
"""Contacts store client — mock at the HTTP boundary (``service.requests``).

Two best-effort calls: ``resolve`` (ask the hub before paying BetterContact) and
``contribute`` (give back what we find, non-EU only, registering on first use).
"""
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import requests

from openoutreach.contacts import service
from openoutreach.core.models import SiteConfig
from tests.factories import LeadFactory


def _resp(status_code=200, body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.raise_for_status.side_effect = (
        None if status_code < 400 else requests.HTTPError(str(status_code))
    )
    return resp


def _config(token="tok", url="", country_code="us"):
    # country_code is the operator's jurisdiction — the give-back gate. Default
    # non-EEA so contribute proceeds; the EEA-operator test overrides it.
    cfg = SiteConfig.load()
    cfg.contacts_api_token = token
    cfg.contacts_api_url = url
    cfg.country_code = country_code
    cfg.save()
    return cfg


@pytest.fixture(autouse=True)
def _operator(db):
    """The register path stamps the operator's email; give it one to find."""
    from tests.factories import UserFactory

    return UserFactory(username="me", email="me@x.com")


# ── resolve ──────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestResolve:
    def test_no_token_returns_none_without_a_call(self):
        _config(token="")
        lead = LeadFactory(profile_url="jane-doe")
        with patch.object(service.requests, "get") as get:
            assert service.resolve(lead) is None
        get.assert_not_called()

    def test_hit_returns_email(self):
        _config()
        lead = LeadFactory(profile_url="jane-doe")
        body = {"public_identifier": "jane-doe", "emails": ["jane@acme.com"]}
        with patch.object(service.requests, "get", return_value=_resp(200, body)):
            assert service.resolve(lead) == "jane@acme.com"

    def test_hit_with_multiple_emails_takes_first(self):
        _config()
        lead = LeadFactory(profile_url="jane-doe")
        body = {"public_identifier": "jane-doe", "emails": ["jane@acme.com", "j@personal.com"]}
        with patch.object(service.requests, "get", return_value=_resp(200, body)):
            assert service.resolve(lead) == "jane@acme.com"

    def test_hit_with_empty_emails_returns_none(self):
        _config()
        lead = LeadFactory(profile_url="jane-doe")
        with patch.object(service.requests, "get", return_value=_resp(200, {"emails": []})):
            assert service.resolve(lead) is None

    def test_miss_returns_none(self):
        _config()
        lead = LeadFactory()
        with patch.object(service.requests, "get", return_value=_resp(404, {})):
            assert service.resolve(lead) is None

    def test_outage_returns_none(self):
        _config()
        lead = LeadFactory()
        with patch.object(
            service.requests, "get", side_effect=requests.ConnectionError("boom"),
        ):
            assert service.resolve(lead) is None


# ── contribute ───────────────────────────────────────────────────────


@pytest.mark.django_db
class TestContribute:
    def test_empty_emails_is_a_noop(self):
        _config()
        lead = LeadFactory(country_code="in")
        with patch.object(service.requests, "post") as post:
            service.contribute(lead, [], service.ORIGIN_BETTERCONTACT)
        post.assert_not_called()

    def test_eea_lead_is_skipped_client_side(self):
        _config()
        lead = LeadFactory(country_code="de")
        with patch.object(service.requests, "post") as post:
            service.contribute(lead, ["jane@acme.com"], service.ORIGIN_BETTERCONTACT)
        post.assert_not_called()

    def test_unknown_country_is_skipped(self):
        _config()
        lead = LeadFactory(country_code="")
        with patch.object(service.requests, "post") as post:
            service.contribute(lead, ["jane@acme.com"], service.ORIGIN_BETTERCONTACT)
        post.assert_not_called()

    def test_with_token_posts_the_record(self):
        _config(token="tok")
        lead = LeadFactory(profile_url="jane-doe", country_code="in")
        with patch.object(
            service.requests, "post", return_value=_resp(200, {"accepted": 1, "credits": 7}),
        ) as post:
            # the empty string is filtered out
            service.contribute(lead, ["jane@acme.com", ""], service.ORIGIN_PROFILE_INFO)
        url, kwargs = post.call_args.args[0], post.call_args.kwargs
        assert url.endswith("/api/v2/contribute/")
        assert kwargs["headers"]["Authorization"] == "Bearer tok"
        # The build fields ride along on every record (see TestBuildReporting);
        # this asserts the payload proper.
        record = {k: v for k, v in kwargs["json"].items() if not k.startswith("client_")}
        assert record == {
            "public_identifier": "jane-doe",
            "country_code": "in",
            "emails": ["jane@acme.com"],
            "origin": "profile_info",
        }

    def test_first_contribution_registers_and_persists_token(self):
        _config(token="")
        lead = LeadFactory(profile_url="jane-doe", country_code="br")
        with patch.object(
            service.requests, "post", return_value=_resp(200, {"token": "NEW", "credits": 1}),
        ) as post:
            service.contribute(lead, ["jane@acme.com"], service.ORIGIN_BETTERCONTACT)
        url, kwargs = post.call_args.args[0], post.call_args.kwargs
        assert url.endswith("/api/v2/register/")
        assert kwargs["json"]["operator_email"] == "me@x.com"
        assert kwargs["json"]["origin"] == "bettercontact"  # origin rides the folded register
        assert SiteConfig.load().contacts_api_token == "NEW"

    def test_outage_is_swallowed_and_no_token_stored(self):
        _config(token="")
        lead = LeadFactory(country_code="in")
        with patch.object(
            service.requests, "post", side_effect=requests.ConnectionError("boom"),
        ):
            # must not raise
            service.contribute(lead, ["jane@acme.com"], service.ORIGIN_BETTERCONTACT)
        assert SiteConfig.load().contacts_api_token == ""

    def test_eea_operator_contributes_nothing(self):
        """An operator inside the EEA/UK/CH does not give back (jurisdiction gate)."""
        cfg = _config(token="tok")
        cfg.country_code = "de"
        cfg.save()
        lead = LeadFactory(country_code="in")
        with patch.object(service.requests, "post") as post:
            service.contribute(lead, ["jane@acme.com"], service.ORIGIN_BETTERCONTACT)
        post.assert_not_called()

    def test_cached_embedding_rides_along(self):
        _config(token="tok")
        lead = LeadFactory(profile_url="jane-doe", country_code="in")
        lead.embedding_array = np.arange(384, dtype=np.float32)
        with patch.object(
            service.requests, "post", return_value=_resp(200, {"accepted": 1, "credits": 7}),
        ) as post:
            service.contribute(lead, ["jane@acme.com"], service.ORIGIN_BETTERCONTACT)
        assert post.call_args.kwargs["json"]["embedding"] == list(range(384))

    def test_uncached_embedding_is_omitted(self):
        _config(token="tok")
        lead = LeadFactory(country_code="in")  # no embedding cached
        with patch.object(
            service.requests, "post", return_value=_resp(200, {"accepted": 1, "credits": 7}),
        ) as post:
            service.contribute(lead, ["jane@acme.com"], service.ORIGIN_BETTERCONTACT)
        assert "embedding" not in post.call_args.kwargs["json"]


# ── which build sent it ──────────────────────────────────────────────


@pytest.mark.django_db
class TestBuildReporting:
    """The client names its build; the hub decides what that name means."""

    def test_contribute_sends_the_commit_sha_and_dirty_flag(self):
        _config()
        lead = LeadFactory(profile_url="jane-doe", country_code="us")
        with patch.object(service.version, "commit_sha", return_value="a" * 40), \
             patch.object(service.version, "is_dirty", return_value=True), \
             patch.object(service.requests, "post", return_value=_resp(body={"credits": 1})) as post:
            service.contribute(lead, ["jane@acme.com"], service.ORIGIN_BETTERCONTACT)
        body = post.call_args.kwargs["json"]
        assert body["client_sha"] == "a" * 40
        assert body["client_dirty"] is True

    def test_undeterminable_dirtiness_is_omitted_not_sent_as_false(self):
        _config()
        lead = LeadFactory(profile_url="jane-doe", country_code="us")
        with patch.object(service.version, "commit_sha", return_value="a" * 40), \
             patch.object(service.version, "is_dirty", return_value=None), \
             patch.object(service.requests, "post", return_value=_resp(body={"credits": 1})) as post:
            service.contribute(lead, ["jane@acme.com"], service.ORIGIN_BETTERCONTACT)
        assert "client_dirty" not in post.call_args.kwargs["json"]

    def test_every_call_carries_the_version_user_agent(self):
        """Including resolve, which never reaches a stored row."""
        _config()
        lead = LeadFactory(profile_url="jane-doe")
        with patch.object(service.version, "version_string", return_value="2026.08.07+gabc1234"), \
             patch.object(service.requests, "get", return_value=_resp(body={"emails": []})) as get:
            service.resolve(lead)
        assert get.call_args.kwargs["headers"]["User-Agent"] == "OpenOutreach/2026.08.07+gabc1234"

    def test_register_carries_the_build_of_the_first_contribution(self):
        _config(token="")
        lead = LeadFactory(profile_url="jane-doe", country_code="us")
        with patch.object(service.version, "commit_sha", return_value="b" * 40), \
             patch.object(service.requests, "post",
                          return_value=_resp(body={"token": "t", "credits": 1})) as post:
            service.contribute(lead, ["jane@acme.com"], service.ORIGIN_BETTERCONTACT)
        assert post.call_args.kwargs["json"]["client_sha"] == "b" * 40
