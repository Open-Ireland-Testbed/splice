import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from splice.db.catalog_repository import prune_catalog_paths, upsert_catalog_paths
from splice.db.models import Base, CatalogPath, Device, Job, ListInstance, PathAlias


def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


def _entry(path, keyword="leaf"):
    return {
        "path": path,
        "filter_xml": "<filter/>",
        "namespace_map": {"p1": "urn:test"},
        "keyword": keyword,
        "config": False,
    }


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_prune_deletes_stale_paths_and_dependents():
    engine = create_engine(_db_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dev = Device(host="10.0.0.9", port=830, username="netconf")
        other = Device(host="10.0.0.10", port=830, username="netconf")
        session.add_all([dev, other])
        session.commit()

        upsert_catalog_paths(
            session,
            dev.id,
            None,
            [_entry("/p1:keep"), _entry("/p1:stale"), _entry("/p1:stale-list", "list")],
        )
        # same stale path on ANOTHER device must survive (device scoping)
        upsert_catalog_paths(session, other.id, None, [_entry("/p1:stale")])

        session.add_all(
            [
                PathAlias(device_id=dev.id, canonical_path="/p1:stale", alias_path="/stale"),
                PathAlias(device_id=dev.id, canonical_path="/p1:keep", alias_path="/keep"),
                # keyed job whose BASE path survives must survive
                Job(device_id=dev.id, canonical_path="/p1:keep[name=a]", interval_sec=10),
                Job(device_id=dev.id, canonical_path="/p1:stale", interval_sec=10),
                # job outside the catalog (e.g. /pm or onboarded) must survive
                Job(device_id=dev.id, canonical_path="/pm/otnpm-data", interval_sec=10),
                ListInstance(
                    device_id=dev.id,
                    list_path="/p1:stale-list",
                    key_values={"name": "x"},
                ),
            ]
        )
        session.commit()

        counts = prune_catalog_paths(session, dev.id, {"/p1:keep"})

        assert counts == {
            "catalog_paths": 2,
            "path_aliases": 1,
            "jobs": 1,
            "list_instances": 1,
        }
        remaining_catalog = {
            r.canonical_path
            for r in session.query(CatalogPath).filter_by(device_id=dev.id)
        }
        assert remaining_catalog == {"/p1:keep"}
        remaining_jobs = {
            r.canonical_path for r in session.query(Job).filter_by(device_id=dev.id)
        }
        assert remaining_jobs == {"/p1:keep[name=a]", "/pm/otnpm-data"}
        # other device untouched
        assert session.query(CatalogPath).filter_by(device_id=other.id).count() == 1


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_prune_noop_when_nothing_stale():
    engine = create_engine(_db_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dev = Device(host="10.0.0.11", port=830, username="netconf")
        session.add(dev)
        session.commit()
        upsert_catalog_paths(session, dev.id, None, [_entry("/p1:keep")])
        counts = prune_catalog_paths(session, dev.id, {"/p1:keep"})
        assert all(v == 0 for v in counts.values())
