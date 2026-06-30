import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from splice.db.models import Base, Device, PathAlias
from splice.db.alias_repository import upsert_aliases


def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_upsert_aliases():
    engine = create_engine(_db_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dev = Device(host="10.0.0.5", port=830, username="netconf")
        session.add(dev)
        session.commit()

        alias_map = {
            "/p1:system/p1:state": "/system/state",
            "/p1:system/p1:config": "/system/config",
        }

        count = upsert_aliases(session, dev.id, alias_map)
        assert count == 2

        rows = session.query(PathAlias).order_by(PathAlias.alias_path).all()
        assert rows[0].alias_path == "/system/config"

        alias_map["/p1:system/p1:state"] = "/system/state-2"
        count2 = upsert_aliases(session, dev.id, alias_map)
        assert count2 == 2

        row = session.query(PathAlias).filter_by(canonical_path="/p1:system/p1:state").one()
        assert row.alias_path == "/system/state-2"
