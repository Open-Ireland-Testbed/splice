"""Tests for expander module — XML filter building and path expansion."""

from splice.expander import build_filter_xml_for_path


class TestBuildFilterXmlEscaping:
    """Verify that key values with XML-special characters are properly escaped."""

    def test_escapes_angle_brackets_and_ampersand(self):
        canonical_path = "/mod:container/mod:list"
        namespace_map = {"urn:example:mod": "mod"}
        list_keys = {"/mod:container/mod:list": ["name"]}
        instance_map = {"/mod:container/mod:list": {"name": "<b>&evil</b>"}}

        xml = build_filter_xml_for_path(
            canonical_path, namespace_map, list_keys, instance_map,
        )

        # The escaped value must appear in the output
        assert "&lt;b&gt;&amp;evil&lt;/b&gt;" in xml
        # The raw unescaped value must NOT appear
        assert "<b>&evil</b>" not in xml


def test_strip_predicates_handles_raw_slash_in_key_value():
    from splice.expander import strip_predicates

    # raw '/' inside a predicate value must not become path segments
    assert (
        strip_predicates("/a/iface[name=1/2/n1/ot400]/state") == "/a/iface/state"
    )
    assert strip_predicates("/foo[a=1][b=2/3]/bar") == "/foo/bar"
    # encoded values and predicate-free paths unchanged
    assert strip_predicates("/a/iface[name=1%2F2]/state") == "/a/iface/state"
    assert strip_predicates("/p1:a/p2:b") == "/p1:a/p2:b"
