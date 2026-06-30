import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from splice.db.last_value_repository import (
    delete_last_values,
    fetch_last_values,
    upsert_last_values,
)
from splice.db.models import Base, Device


def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_delete_last_values_exact_and_descendants():
    engine = create_engine(_db_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dev = Device(host="10.0.0.10", port=830, username="u")
        session.add(dev)
        session.commit()

        upsert_last_values(
            session,
            dev.id,
            [
                {"path": "/system/config/hostname", "value": "r1", "timestamp_ns": 1},
                {"path": "/system/config/domain-name", "value": "example.net", "timestamp_ns": 2},
                {
                    "path": "/interfaces/interface[name=eth0]/config/enabled",
                    "value": True,
                    "timestamp_ns": 3,
                },
                {
                    "path": "/interfaces/interface[name=eth1]/config/enabled",
                    "value": False,
                    "timestamp_ns": 4,
                },
            ],
        )

        removed = delete_last_values(
            session,
            dev.id,
            ["/p1:system/p2:config/p3:hostname", "/interfaces/interface[name=eth0]"],
            include_descendants=True,
        )
        assert removed == 2

        remaining = fetch_last_values(session, dev.id)
        assert "/system/config/hostname" not in remaining
        assert "/interfaces/interface[name=eth0]/config/enabled" not in remaining
        assert "/system/config/domain-name" in remaining
        assert "/interfaces/interface[name=eth1]/config/enabled" in remaining
