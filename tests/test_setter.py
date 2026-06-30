import pytest

from splice.netconf.setter import (
    NETCONF_BASE_NS,
    SetMutation,
    build_config_xml,
    build_edit_config_rpc,
    parse_canonical_path,
)


NSMAP = {"urn:example:if": "p1"}


def test_build_config_xml_merges_common_ancestors():
    mutations = [
        SetMutation(
            canonical_path="/p1:interfaces/p1:interface[name=eth0]/p1:config/p1:mtu",
            operation="merge",
            value=9000,
            namespace_map=NSMAP,
        ),
        SetMutation(
            canonical_path="/p1:interfaces/p1:interface[name=eth0]/p1:config/p1:description",
            operation="merge",
            value="uplink",
            namespace_map=NSMAP,
        ),
    ]

    xml = build_config_xml(mutations)
    assert xml.count("<p1:interfaces>") == 1
    assert xml.count("<p1:interface>") == 1
    assert "<p1:name>eth0</p1:name>" in xml
    assert '<p1:mtu nc:operation="merge">9000</p1:mtu>' in xml
    assert '<p1:description nc:operation="merge">uplink</p1:description>' in xml


def test_build_config_xml_distinguishes_keyed_list_instances():
    mutations = [
        SetMutation(
            canonical_path="/p1:interfaces/p1:interface[name=eth0]/p1:config/p1:enabled",
            operation="merge",
            value=True,
            namespace_map=NSMAP,
        ),
        SetMutation(
            canonical_path="/p1:interfaces/p1:interface[name=eth1]/p1:config/p1:enabled",
            operation="merge",
            value=False,
            namespace_map=NSMAP,
        ),
    ]

    xml = build_config_xml(mutations)
    assert xml.count("<p1:interface>") == 2
    assert xml.count("<p1:name>eth0</p1:name>") == 1
    assert xml.count("<p1:name>eth1</p1:name>") == 1
    assert '<p1:enabled nc:operation="merge">true</p1:enabled>' in xml
    assert '<p1:enabled nc:operation="merge">false</p1:enabled>' in xml


def test_build_config_xml_delete_emits_operation_without_value():
    mutations = [
        SetMutation(
            canonical_path="/p1:interfaces/p1:interface[name=eth0]/p1:config/p1:description",
            operation="delete",
            namespace_map=NSMAP,
        )
    ]
    xml = build_config_xml(mutations)
    assert '<p1:description nc:operation="delete"/>' in xml


def test_build_config_xml_rejects_conflicting_duplicate_path():
    mutations = [
        SetMutation(
            canonical_path="/p1:interfaces/p1:interface[name=eth0]/p1:config/p1:mtu",
            operation="merge",
            value=9000,
            namespace_map=NSMAP,
        ),
        SetMutation(
            canonical_path="/p1:interfaces/p1:interface[name=eth0]/p1:config/p1:mtu",
            operation="replace",
            value=9100,
            namespace_map=NSMAP,
        ),
    ]
    with pytest.raises(ValueError, match="conflicting duplicate mutation"):
        build_config_xml(mutations)


def test_build_config_xml_requires_namespace_mapping_for_path_prefix():
    mutations = [
        SetMutation(
            canonical_path="/p1:interfaces/p1:interface[name=eth0]/p1:config/p1:mtu",
            operation="merge",
            value=9000,
            namespace_map=None,
        )
    ]
    with pytest.raises(ValueError, match="missing namespace mappings"):
        build_config_xml(mutations)


def test_parse_canonical_path_decodes_predicate_values():
    parsed = parse_canonical_path("/p1:interfaces/p1:interface[name=eth0%2F1]")
    assert parsed[-1].tag == "p1:interface"
    assert parsed[-1].key_values == (("name", "eth0/1"),)


def test_build_edit_config_rpc_wraps_config_payload():
    config_xml = '<config xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0"/>'
    rpc = build_edit_config_rpc(config_xml, target="running")
    assert '<edit-config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">' in rpc
    assert "<target><running/></target>" in rpc
    assert config_xml in rpc


def test_build_config_xml_includes_netconf_base_namespace_on_config_element():
    """The <config> element MUST be in the NETCONF base namespace (RFC 6241 s7.2).

    Without this, strict ConfD-based devices (ADVA, Lumentum) reject the
    edit-config with 'Missing element namespace' because ncclient/lxml
    serializes <config> without a namespace when only xmlns:nc is declared.
    Declaring xmlns=NETCONF_BASE_NS ensures the <config> element is in the
    NETCONF base namespace on the wire, regardless of which prefix lxml uses.
    """
    mutations = [
        SetMutation(
            canonical_path="/p1:interfaces/p1:interface[name=eth0]/p1:config/p1:mtu",
            operation="merge",
            value=1500,
            namespace_map=NSMAP,
        )
    ]
    xml = build_config_xml(mutations)
    assert xml.startswith(f'<config xmlns="{NETCONF_BASE_NS}"')
    assert 'xmlns:nc="' in xml
    assert 'xmlns:p1="' in xml


def test_set_rejects_xml_injection_in_element_name():
    import pytest
    from splice.netconf.setter import SetMutation, build_config_xml

    with pytest.raises(ValueError, match="invalid XML name"):
        build_config_xml([
            SetMutation(
                canonical_path="/foo><evil/>x",
                operation="merge",
                value="1",
                namespace_map={},
            )
        ])


def test_set_rejects_xml_injection_in_predicate_key():
    import pytest
    from splice.netconf.setter import SetMutation, build_config_xml

    with pytest.raises(ValueError, match="invalid XML name"):
        build_config_xml([
            SetMutation(
                canonical_path="/iface[na>me=eth0]/enabled",
                operation="merge",
                value="true",
                namespace_map={},
            )
        ])


def test_set_predicate_value_still_escaped_not_rejected():
    # Values (not names) may contain XML metacharacters; they must be escaped,
    # not rejected.
    from splice.netconf.setter import SetMutation, build_config_xml

    xml = build_config_xml([
        SetMutation(
            canonical_path="/p1:iface[p1:name=a<b&c]/p1:enabled",
            operation="merge",
            value="x>y",
            namespace_map={"urn:test": "p1"},
        )
    ])
    assert "a&lt;b&amp;c" in xml
    assert "x&gt;y" in xml
    assert "<evil" not in xml
