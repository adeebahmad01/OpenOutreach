# tests/conftest.py
from unittest.mock import patch

import numpy as np
import pytest

from openoutreach.core.management.setup_crm import setup_crm
from tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _ensure_crm_data(db):
    """
    Ensure CRM bootstrap data exists before every test.
    Uses `db` fixture (not transactional_db) for compatibility.
    Since transaction=True tests rollback, we re-create data each time.
    """
    setup_crm()


@pytest.fixture(autouse=True)
def _mock_embeddings(request):
    """Stub fastembed so tests don't need the ONNX model."""
    if "no_embed_mock" in request.keywords:
        yield
    else:
        with patch("openoutreach.core.ml.embeddings.embed_text", return_value=np.ones(384)):
            yield


@pytest.fixture(autouse=True)
def _open_sending_window():
    """Hold the operator's sending window open for the whole suite.

    Otherwise every test that sends a first email passes or fails by the wall
    clock of the machine running it — green at 10:00, red at midnight and all
    weekend. Tests that care about the window patch it back closed themselves;
    the window's own rules are tested against explicit datetimes in
    ``tests/test_sending_window.py``.
    """
    with patch("openoutreach.emails.models.mailbox.within_sending_window", return_value=True):
        yield


@pytest.fixture
def operator(db):
    """The onboarded operator — what ``core.operator.get_active_user()`` will find."""
    return UserFactory(username="testuser", email="testuser@example.com")


@pytest.fixture
def campaign(db, operator):
    """The campaign under test, owned by the operator.

    Steps and pipeline functions take a campaign now; the operator is looked up
    (``core/operator.py``) rather than threaded through, so nothing carries a
    session object any more.
    """
    from openoutreach.core.models import Campaign

    row = Campaign.objects.first() or Campaign.objects.create(name="Email Outreach")
    row.users.add(operator)
    return row
