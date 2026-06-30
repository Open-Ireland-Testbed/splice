import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from splice.db.models import Base, Device
from splice.db.job_repository import fetch_job_intervals, upsert_jobs


def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_job_intervals_roundtrip():
    engine = create_engine(_db_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dev = Device(host="10.0.0.6", port=830, username="netconf")
        session.add(dev)
        session.commit()

        jobs = {
            "/p1:system": 30,
            "/p1:system/p1:state": 5,
        }
        count = upsert_jobs(session, dev.id, jobs)
        assert count == 2

        intervals = fetch_job_intervals(session, dev.id)
        assert intervals["/p1:system"] == 30
        assert intervals["/p1:system/p1:state"] == 5
