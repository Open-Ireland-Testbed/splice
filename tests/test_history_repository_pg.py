import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from splice.db.models import Base, Device, TelemetryHistory
from splice.db.history_repository import insert_history


def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_insert_history():
    engine = create_engine(_db_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dev = Device(host="10.0.0.9", port=830, username="netconf")
        session.add(dev)
        session.commit()

        updates = [
            {"path": "/a", "value": 1, "timestamp_ns": 1},
            {"path": "/b", "value": 2, "timestamp_ns": 2},
        ]
        count = insert_history(session, dev.id, updates)
        assert count == 2

        rows = session.query(TelemetryHistory).all()
        assert len(rows) == 2
