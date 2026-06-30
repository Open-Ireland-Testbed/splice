from splice.netconf.yang_fetcher import extract_schema_text


def test_extract_schema_text_simple():
    xml = """
    <rpc-reply>
      <data>module foo { namespace \'urn:foo\'; }</data>
    </rpc-reply>
    """
    out = extract_schema_text(xml)
    assert "module foo" in out


def test_extract_schema_text_namespaced():
    xml = """
    <rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <data xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">
        module bar { namespace 'urn:bar'; }
      </data>
    </rpc-reply>
    """
    out = extract_schema_text(xml)
    assert "module bar" in out
