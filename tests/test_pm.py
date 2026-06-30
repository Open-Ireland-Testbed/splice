from splice.pm import parse_pm_current_data

NS_BULK = "http://www.advaoptical.com/aos/netconf/aos-core-bulk-data"
NS_ME = "http://www.advaoptical.com/aos/netconf/aos-core-me-bulk"
NS_FACPM = "http://www.advaoptical.com/aos/netconf/aos-core-facpm-data"
NS_OTNPM = "http://www.advaoptical.com/aos/netconf/aos-core-otnpm-data"
NS_EQPM = "http://www.advaoptical.com/aos/netconf/aos-core-eqpm-data"


def _interface_pm(name: str, ber: str) -> str:
    return f"""
      <interface xmlns="{NS_FACPM}">
        <name>{name}</name>
        <physical-interface>
          <lr-physm-optical>
            <pm-monitored-entity>
              <pm-current-data xmlns="{NS_OTNPM}">
                <name>FEC</name>
                <bin-interval>acor-pmt:interval-indefinite</bin-interval>
                <montype-monval>
                  <mon-type>acor-factt:fec-ber</mon-type>
                  <mon-val>{ber}</mon-val>
                </montype-monval>
              </pm-current-data>
            </pm-monitored-entity>
          </lr-physm-optical>
        </physical-interface>
      </interface>"""


def _reply(body: str) -> str:
    return f"""
    <rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <data>
        <bulk-data xmlns="{NS_BULK}">
          <managed-element xmlns="{NS_ME}">
            <entity-name>1</entity-name>
            {body}
          </managed-element>
        </bulk-data>
      </data>
    </rpc-reply>"""


def test_instances_get_distinct_keyed_paths():
    xml = _reply(_interface_pm("1/2/n1/ot400", "1e-7") + _interface_pm("1/2/n2/ot400", "3e-7"))
    updates = parse_pm_current_data(xml, live_token="interval-indefinite")
    paths = sorted(u["path"] for u in updates)
    assert len(paths) == 2
    assert paths[0] != paths[1]
    # single-bracket, comma-joined, sorted keys, percent-encoded values
    assert (
        "/pm/otnpm-data[interface=1%2F2%2Fn1%2Fot400,managed-element=1]/FEC/acor-factt:fec-ber"
        in paths
    )
    assert (
        "/pm/otnpm-data[interface=1%2F2%2Fn2%2Fot400,managed-element=1]/FEC/acor-factt:fec-ber"
        in paths
    )


def test_instance_keys_in_meta():
    xml = _reply(_interface_pm("1/2/n1/ot400", "1e-7"))
    (update,) = parse_pm_current_data(xml, live_token="interval-indefinite")
    assert update["meta"]["instance"] == {
        "interface": "1/2/n1/ot400",
        "managed-element": "1",
    }
    assert update["value"] == 1e-7


def test_entity_own_name_is_not_an_instance_key():
    xml = _reply(_interface_pm("1/2/n1/ot400", "1e-7"))
    (update,) = parse_pm_current_data(xml, live_token="interval-indefinite")
    # "FEC" is the pm-current-data's own name (a path segment), not a key
    assert "FEC" not in str(update["meta"]["instance"])
    assert update["path"].endswith("/FEC/acor-factt:fec-ber")


def test_index_and_id_key_leaves():
    body = f"""
      <card xmlns="{NS_EQPM}">
        <name>1/1</name>
        <smdm>
          <cpu>
            <index>0</index>
            <pm-monitored-entity>
              <pm-current-data>
                <name>Cpu</name>
                <bin-interval>acor-pmt:interval-indefinite</bin-interval>
                <montype-monval>
                  <mon-type>util</mon-type>
                  <mon-val>42</mon-val>
                </montype-monval>
              </pm-current-data>
            </pm-monitored-entity>
          </cpu>
        </smdm>
      </card>"""
    xml = _reply(body)
    (update,) = parse_pm_current_data(xml, live_token="interval-indefinite")
    assert update["meta"]["instance"] == {
        "card": "1/1",
        "cpu": "0",
        "managed-element": "1",
    }
    assert update["path"] == (
        "/pm/eqpm-data[card=1%2F1,cpu=0,managed-element=1]/Cpu/util"
    )


def test_empty_mon_val_skipped():
    xml = _reply(_interface_pm("1/2/n1/ot400", ""))
    updates = parse_pm_current_data(xml, live_token="interval-indefinite")
    assert updates == []


def test_live_token_filters_bins():
    body = _interface_pm("1/2/n1/ot400", "1e-7").replace(
        "interval-indefinite", "interval-15min"
    )
    xml = _reply(body)
    assert parse_pm_current_data(xml, live_token="interval-indefinite") == []
    assert len(parse_pm_current_data(xml, live_token=None)) == 1


def test_unkeyed_reply_keeps_plain_paths():
    xml = f"""
    <rpc-reply>
      <data>
        <pm-current-data xmlns="{NS_EQPM}">
          <name>Shelf</name>
          <bin-interval>indefinite</bin-interval>
          <montype-monval>
            <mon-type>temperature</mon-type>
            <mon-val>35.5</mon-val>
          </montype-monval>
        </pm-current-data>
      </data>
    </rpc-reply>"""
    (update,) = parse_pm_current_data(xml)
    assert update["path"] == "/pm/eqpm-data/Shelf/temperature"
    assert update["meta"]["instance"] == {}


def test_keyed_path_round_trips_through_gnmi_server():
    from splice.gnmi_server import _path_from_str, _path_to_str

    xml = _reply(_interface_pm("1/2/n1/ot400", "1e-7"))
    (update,) = parse_pm_current_data(xml, live_token="interval-indefinite")
    path = _path_from_str(update["path"])
    keyed = next(e for e in path.elem if e.key)
    assert keyed.name == "otnpm-data"
    assert dict(keyed.key) == {
        "interface": "1/2/n1/ot400",
        "managed-element": "1",
    }
    assert _path_to_str(path) == update["path"]


def test_sentinel_mon_vals_skipped():
    # "no measurement" placeholders must be dropped like "" — publishing them
    # as strings poisons TSDB field types
    for sentinel in ("NA", "n/a", "-", "--", "unavailable", "  NA  "):
        xml = _reply(_interface_pm("1/2/n1/ot400", sentinel))
        assert parse_pm_current_data(xml, live_token="interval-indefinite") == []


def test_legitimate_string_values_still_published():
    xml = _reply(_interface_pm("1/2/n1/ot400", "normal-operation"))
    (update,) = parse_pm_current_data(xml, live_token="interval-indefinite")
    assert update["value"] == "normal-operation"


# ---------------------------------------------------------------------------
# Selector grammar (shared by PM discovery and doctor)
# ---------------------------------------------------------------------------

def test_parse_selector_single_equality():
    from splice.pm import parse_selector

    clauses, ops = parse_selector("name='1/2/n2/ot400'")
    assert clauses == [("name", "=", "1/2/n2/ot400")]
    assert ops == []


def test_parse_selector_and_or_with_regex():
    from splice.pm import parse_selector

    clauses, ops = parse_selector("name~'ot[0-9]+$' and type='line' or name='x'")
    assert clauses == [
        ("name", "~", "ot[0-9]+$"),
        ("type", "=", "line"),
        ("name", "=", "x"),
    ]
    assert ops == ["and", "or"]


def test_parse_selector_invalid_raises():
    import pytest
    from splice.pm import parse_selector

    with pytest.raises(ValueError):
        parse_selector("name=unquoted")


def test_match_selector_equality_and_regex():
    from splice.pm import match_selector, parse_selector

    clauses, ops = parse_selector("name~'/ot[0-9]+$'")
    assert match_selector({"name": "1/2/n1/ot400"}, clauses, ops)
    assert not match_selector({"name": "1/2/n1"}, clauses, ops)
    # missing key never matches
    assert not match_selector({"entity-name": "1"}, clauses, ops)


def test_match_selector_and_or_logic():
    from splice.pm import match_selector, parse_selector

    clauses, ops = parse_selector("name='a' and type='line'")
    assert match_selector({"name": "a", "type": "line"}, clauses, ops)
    assert not match_selector({"name": "a", "type": "client"}, clauses, ops)

    clauses, ops = parse_selector("name='a' or name='b'")
    assert match_selector({"name": "b"}, clauses, ops)
    assert not match_selector({"name": "c"}, clauses, ops)


def test_match_selector_empty_clauses_matches_nothing():
    from splice.pm import match_selector

    assert not match_selector({"name": "a"}, [], [])
