import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from splice.db.models import Base, Device, CatalogPath, PathAlias


def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_create_and_insert_models():
    engine = create_engine(_db_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dev = Device(host="10.0.0.1", port=830, username="netconf")
        session.add(dev)
        session.commit()

        cp = CatalogPath(
            device_id=dev.id,
            module_id=None,
            canonical_path="/p1:system/p1:state",
            filter_xml="<filter><p1:system xmlns:p1=\"urn:example\"/></filter>",
            namespace_map={"p1": "urn:example"},
            keyword="container",
            config=False,
        )
        session.add(cp)

        alias = PathAlias(
            device_id=dev.id,
            canonical_path="/p1:system/p1:state",
            alias_path="/system/state",
            source="generated",
        )
        session.add(alias)
        session.commit()

        assert dev.id is not None
        assert cp.id is not None
        assert alias.id is not None
