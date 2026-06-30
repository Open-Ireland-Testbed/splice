from splice.discovery import (
    parse_yang_library,
    parse_netconf_monitoring,
    select_supported_modules,
)


def test_parse_yang_library_rfc8525():
    xml = """
    <data>
      <yang-library xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-library">
        <module-set>
          <name>complete</name>
          <module>
            <name>ietf-system</name>
            <revision>2014-08-06</revision>
            <namespace>urn:ietf:params:xml:ns:yang:ietf-system</namespace>
            <conformance-type>implement</conformance-type>
            <feature>local-users</feature>
          </module>
        </module-set>
      </yang-library>
    </data>
    """
    out = parse_yang_library(xml)
    assert out is not None
    assert out["source"] == "ietf-yang-library"
    assert out["modules"][0]["name"] == "ietf-system"


def test_parse_yang_library_rfc7895():
    xml = """
    <data>
      <modules-state xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-library">
        <module>
          <name>ietf-interfaces</name>
          <revision>2014-05-08</revision>
          <namespace>urn:ietf:params:xml:ns:yang:ietf-interfaces</namespace>
          <conformance-type>implement</conformance-type>
        </module>
      </modules-state>
    </data>
    """
    out = parse_yang_library(xml)
    assert out is not None
    assert out["source"] == "ietf-yang-library (RFC 7895)"
    assert out["modules"][0]["name"] == "ietf-interfaces"


def test_parse_netconf_monitoring():
    xml = """
    <data>
      <netconf-state xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">
        <schemas>
          <schema>
            <identifier>ietf-netconf</identifier>
            <version>2011-06-01</version>
            <namespace>urn:ietf:params:xml:ns:netconf:base:1.0</namespace>
            <format>yang</format>
            <location>NETCONF</location>
          </schema>
        </schemas>
      </netconf-state>
    </data>
    """
    out = parse_netconf_monitoring(xml)
    assert out is not None
    assert out["source"] == "ietf-netconf-monitoring"
    assert out["modules"][0]["name"] == "ietf-netconf"


def test_select_supported_modules_prefers_yang_library():
    xml = """
    <data>
      <yang-library xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-library">
        <module-set>
          <name>complete</name>
          <module>
            <name>ietf-system</name>
          </module>
        </module-set>
      </yang-library>
      <netconf-state xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">
        <schemas>
          <schema>
            <identifier>ietf-netconf</identifier>
          </schema>
        </schemas>
      </netconf-state>
    </data>
    """
    source, mods = select_supported_modules(xml)
    assert source == "ietf-yang-library"
    assert mods[0]["name"] == "ietf-system"
