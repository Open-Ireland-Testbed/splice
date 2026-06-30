import os
import pytest
from types import SimpleNamespace

from splice.config import AppConfig, DeviceConfig, DBConfig, GNMIConfig, PollerConfig
from splice.runtime import run_discovery, run_fetch_yang, run_build_catalog, run_init_jobs
from splice.db.session import make_session_factory
from splice.db.models import Base
from splice.db.catalog_query import fetch_catalog_paths


def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


class FakeReply:
    def __init__(self, data_xml=None, xml=None):
        self.data_xml = data_xml or ""
        self.xml = xml or ""


class FakeManager:
    def __init__(self):
        self._yang = """
        <data>
          <yang-library xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-library">
            <module-set>
              <name>complete</name>
              <module>
                <name>test-mod</name>
                <revision>2024-01-01</revision>
                <namespace>urn:test</namespace>
              </module>
            </module-set>
          </yang-library>
        </data>
        """
        self._schema = """
        <rpc-reply>
          <data>module test-mod { namespace 'urn:test'; prefix t; container system { leaf hostname { type string; } } }</data>
        </rpc-reply>
        """

    def get(self, _filter):
        return FakeReply(data_xml=self._yang)

    def dispatch(self, _elem):
        return FakeReply(xml=self._schema)


@pytest.mark.skipif(not _db_url(), reason="TEST_DATABASE_URL not set")
def test_cli_pipeline_smoke(tmp_path):
    cfg = AppConfig(
        device=DeviceConfig(host="10.0.0.10", port=830, username="netconf", password="x"),
        db=DBConfig(url=_db_url()),
        yang_dir=str(tmp_path / "yang"),
        gnmi=GNMIConfig(),
        poller=PollerConfig(default_interval_sec=30),
    )

    SessionFactory = make_session_factory(cfg.db.url)
    with SessionFactory() as session:
        Base.metadata.drop_all(session.get_bind())
        Base.metadata.create_all(session.get_bind())

    mgr = FakeManager()
    with SessionFactory() as session:
        assert run_discovery(cfg, mgr, session) == 1
    with SessionFactory() as session:
        assert run_fetch_yang(cfg, mgr, session) == 1
    with SessionFactory() as session:
        assert run_build_catalog(cfg, session, include_config=True) >= 2
        paths = fetch_catalog_paths(session, 1)
        assert any("/p1:system" in p for p in paths)
        assert run_init_jobs(session, 1, paths, 30) > 0
