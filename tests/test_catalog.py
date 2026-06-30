import tempfile
from pathlib import Path

from splice.catalog import build_catalog


def test_build_catalog_basic():
    yang = """
    module test-mod {
      namespace "urn:test";
      prefix "t";
      container "system" {
        leaf "hostname" { type string; }
      }
    }
    """
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "test-mod.yang"
        p.write_text(yang)
        cat = build_catalog(str(p.parent), include_config=True)

    # Expect container + leaf
    paths = [c["path"] for c in cat]
    assert any("/p1:system" == p for p in paths)
    assert any("/p1:system/p1:hostname" == p for p in paths)
