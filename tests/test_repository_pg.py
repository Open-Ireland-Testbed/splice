import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from splice.db.models import Base, Device, Module
from splice.db.repository import upsert_modules


def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_upsert_modules():
    engine = create_engine(_db_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dev = Device(host="10.0.0.2", port=830, username="netconf")
        session.add(dev)
        session.commit()

        modules = [
            {
                "name": "ietf-system",
                "revision": "2014-08-06",
                "namespace": "urn:ietf:params:xml:ns:yang:ietf-system",
                "format": "yang",
            }
        ]

        count = upsert_modules(session, dev.id, modules, source="ietf-yang-library")
        assert count == 1

        row = session.query(Module).filter_by(name="ietf-system").one()
        assert row.source == "ietf-yang-library"
        assert row.namespace.endswith("ietf-system")

        # Update the same module
        modules[0]["format"] = "yin"
        count2 = upsert_modules(session, dev.id, modules, source="ietf-netconf-monitoring")
        assert count2 == 1

        row2 = session.query(Module).filter_by(name="ietf-system").one()
        assert row2.format == "yin"
        assert row2.source == "ietf-netconf-monitoring"
