from splice.config import load_config


def test_cache_config(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        """
        device:
          host: 10.0.0.1
        db:
          url: postgresql://user:pass@localhost:5432/db
        cache:
          ttl_sec: 10
          warm_start: false
        """
    )
    c = load_config(str(cfg))
    assert c.cache.ttl_sec == 10
    assert c.cache.warm_start is False
