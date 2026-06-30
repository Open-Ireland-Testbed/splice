import os
import pytest
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from splice.db.models import Base, Device, Module, YangFile
from splice.netconf.yang_fetcher import fetch_and_store_schema


def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


class FakeReply:
    def __init__(self, xml):
        self.xml = xml


class FakeManager:
    def __init__(self, reply_xml: str):
        self._xml = reply_xml

    def dispatch(self, _):
        return FakeReply(self._xml)


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_fetch_and_store_schema(tmp_path):
    engine = create_engine(_db_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dev = Device(host="10.0.0.3", port=830, username="netconf")
        session.add(dev)
        session.commit()

        mod = Module(device_id=dev.id, name="example-mod", revision="2024-01-01")
        session.add(mod)
        session.commit()

        xml = """
        <rpc-reply>
          <data>module example-mod { namespace 'urn:example'; }</data>
        </rpc-reply>
        """
        mgr = FakeManager(xml)

        out_dir = tmp_path / "yang"
        path = fetch_and_store_schema(
            mgr,
            session,
            module_id=mod.id,
            module_name=mod.name,
            revision=mod.revision,
            out_dir=str(out_dir),
        )

        assert path is not None
        assert path.endswith("example-mod@2024-01-01.yang")
        assert (out_dir / "example-mod@2024-01-01.yang").exists()

        row = session.query(YangFile).one()
        assert row.path.endswith("example-mod@2024-01-01.yang")
        assert row.checksum is not None
