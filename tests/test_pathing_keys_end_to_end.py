from splice.netconf.poller import parse_xml_to_kv
from splice.pathing import apply_list_keys


def test_list_key_path_application():
    xml = """
    <rpc-reply>
      <data>
        <interfaces>
          <interface>
            <name>eth0</name>
            <state>
              <mtu>1500</mtu>
            </state>
          </interface>
        </interfaces>
      </data>
    </rpc-reply>
    """
    updates = parse_xml_to_kv(xml, type_map={"/interfaces/interface/state/mtu": "uint16"})
    values = {u["path"]: str(u["value"]) for u in updates}
    list_keys = {"/interfaces/interface": ["name"]}
    keyed = []
    for u in updates:
        u["path"] = apply_list_keys(u["path"], values, list_keys)
        keyed.append(u["path"])
    assert any("interface[name=eth0]" in p for p in keyed)


def test_multi_instance_keyed_list_does_not_collapse():
    """Regression: a single reply with >=2 instances of one keyed list must
    keep each instance distinct and correctly attributed. The old post-hoc
    re-keying collapsed every leaf onto the last-seen key (only eth1 survived)."""
    xml = """
    <rpc-reply>
      <data>
        <interfaces>
          <interface>
            <name>eth0</name>
            <state><mtu>1500</mtu></state>
          </interface>
          <interface>
            <name>eth1</name>
            <state><mtu>9000</mtu></state>
          </interface>
        </interfaces>
      </data>
    </rpc-reply>
    """
    updates = parse_xml_to_kv(
        xml,
        type_map={"/interfaces/interface/state/mtu": "uint16"},
        list_keys={"/interfaces/interface": ["name"]},
    )
    by_path = {u["path"]: u["value"] for u in updates}
    # Both instances present, each leaf under its own key predicate.
    assert by_path["/interfaces/interface[name=eth0]/state/mtu"] == 1500
    assert by_path["/interfaces/interface[name=eth1]/state/mtu"] == 9000
    assert by_path["/interfaces/interface[name=eth0]/name"] == "eth0"
    assert by_path["/interfaces/interface[name=eth1]/name"] == "eth1"
    # No predicate-less collision survives.
    assert "/interfaces/interface/state/mtu" not in by_path


def test_multi_key_list_predicate():
    """Composite-key list: both key leaves appear in the predicate, in key order."""
    xml = """
    <rpc-reply>
      <data>
        <flows>
          <flow>
            <src>a</src>
            <dst>b</dst>
            <bytes>10</bytes>
          </flow>
          <flow>
            <src>a</src>
            <dst>c</dst>
            <bytes>20</bytes>
          </flow>
        </flows>
      </data>
    </rpc-reply>
    """
    updates = parse_xml_to_kv(
        xml,
        list_keys={"/flows/flow": ["src", "dst"]},
    )
    by_path = {u["path"]: u["value"] for u in updates}
    assert by_path["/flows/flow[src=a,dst=b]/bytes"] == "10"
    assert by_path["/flows/flow[src=a,dst=c]/bytes"] == "20"
