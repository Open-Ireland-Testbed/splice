import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from splice.db.models import Base, Device, PathAlias
from splice.db.alias_query import fetch_alias_map


def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_fetch_alias_map():
    engine = create_engine(_db_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dev = Device(host="10.0.0.7", port=830, username="netconf")
        session.add(dev)
        session.commit()

        session.add(
            PathAlias(device_id=dev.id, canonical_path="/p1:system", alias_path="/system")
        )
        session.commit()

        m = fetch_alias_map(session, dev.id)
        assert m["/system"] == "/p1:system"
