import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from splice.db.models import Base, Device, Module
from splice.db.model_query import fetch_models


def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_fetch_models():
    engine = create_engine(_db_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dev = Device(host="10.0.0.8", port=830, username="netconf")
        session.add(dev)
        session.commit()

        session.add(Module(device_id=dev.id, name="ietf-system", revision="2014-08-06"))
        session.commit()

        models = fetch_models(session, dev.id)
        assert models[0]["name"] == "ietf-system"
