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
def _data_dir(settings, tmp_path):
    """Point the data dir at a temp directory, so the lead CSVs never land in the checkout.

    ``DATABASE_PATH`` is what ``core/export.py`` writes beside; the live connection is
    pytest-django's own test database and is unaffected by this.
    """
    settings.DATABASE_PATH = tmp_path / "db.sqlite3"


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
