from splice.netconf.discovery_client import fetch_supported_modules


class FakeReply:
    def __init__(self, data_xml: str):
        self.data_xml = data_xml


class FakeManager:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, _filter):
        if not self._responses:
            raise RuntimeError("no response")
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return FakeReply(resp)


def test_fetch_supported_modules_prefers_yang_library():
    yang_lib = """
    <data>
      <yang-library xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-library">
        <module-set>
          <name>complete</name>
          <module>
            <name>ietf-system</name>
          </module>
        </module-set>
      </yang-library>
    </data>
    """
    monitoring = """
    <data>
      <netconf-state xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">
        <schemas>
          <schema>
            <identifier>ietf-netconf</identifier>
          </schema>
        </schemas>
      </netconf-state>
    </data>
    """
    mgr = FakeManager([yang_lib, monitoring])
    source, mods = fetch_supported_modules(mgr)
    assert source == "ietf-yang-library"
    assert mods[0]["name"] == "ietf-system"
    assert mgr.calls == 1


def test_fetch_supported_modules_fallback_to_monitoring():
    yang_lib_fail = RuntimeError("rpc error")
    monitoring = """
    <data>
      <netconf-state xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">
        <schemas>
          <schema>
            <identifier>ietf-netconf</identifier>
          </schema>
        </schemas>
      </netconf-state>
    </data>
    """
    mgr = FakeManager([yang_lib_fail, monitoring])
    source, mods = fetch_supported_modules(mgr)
    assert source == "ietf-netconf-monitoring"
    assert mods[0]["name"] == "ietf-netconf"
