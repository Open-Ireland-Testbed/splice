import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from splice.db.models import Base, Device, CatalogPath
from splice.db.catalog_repository import upsert_catalog_paths


def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_upsert_catalog_paths():
    engine = create_engine(_db_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dev = Device(host="10.0.0.4", port=830, username="netconf")
        session.add(dev)
        session.commit()

        entries = [
            {
                "path": "/p1:system",
                "filter_xml": "<filter></filter>",
                "namespace_map": {"p1": "urn:test"},
                "keyword": "container",
                "config": False,
            }
        ]
        count = upsert_catalog_paths(session, dev.id, None, entries)
        assert count == 1

        row = session.query(CatalogPath).one()
        assert row.canonical_path == "/p1:system"

        # Update
        entries[0]["filter_xml"] = "<filter><x/></filter>"
        count2 = upsert_catalog_paths(session, dev.id, None, entries)
        assert count2 == 1

        row2 = session.query(CatalogPath).one()
        assert row2.filter_xml == "<filter><x/></filter>"
