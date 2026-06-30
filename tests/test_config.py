import pytest

from splice.config import GNMIConfig, KafkaConfig, TriggerConfig, load_config


def test_load_config(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        """
        device:
          host: 10.0.0.1
          port: 830
          username: u
          password: p
        db:
          url: postgresql://user:pass@localhost:5432/db
        yang_dir: ./yang
        gnmi:
          bind: 0.0.0.0
          port: 9339
        poller:
          default_interval_sec: 12
          rules:
            - pattern: "/edfas"
              interval: 5
        """
    )
    c = load_config(str(cfg))
    assert c.device.host == "10.0.0.1"
    assert c.db.url.startswith("postgresql://")
    assert c.poller.default_interval_sec == 12
    assert c.poller.rules[0]["interval"] == 5


def test_default_expansion_mode_is_direct():
    assert GNMIConfig().expansion_mode == "direct"


def test_load_config_default_expansion_mode(tmp_path):
    """When gnmi section omits expansion_mode, it defaults to 'direct'."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        """
        device:
          host: 10.0.0.1
          username: u
          password: p
        db:
          url: postgresql://user:pass@localhost:5432/db
        gnmi:
          port: 9339
        """
    )
    c = load_config(str(cfg))
    assert c.gnmi.expansion_mode == "direct"


def test_load_config_poller_resilience_settings(tmp_path):
    """Poller resilience settings should load with correct defaults."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        """
        device:
          host: 10.0.0.1
          username: u
          password: p
        db:
          url: postgresql://user:pass@localhost:5432/db
        poller:
          reconnect_delay_sec: 3.0
          circuit_breaker_threshold: 3
          circuit_breaker_cooldown_sec: 120.0
          auth_failure_cooldown_sec: 600.0
        """
    )
    c = load_config(str(cfg))
    assert c.poller.reconnect_delay_sec == 3.0
    assert c.poller.circuit_breaker_threshold == 3
    assert c.poller.circuit_breaker_cooldown_sec == 120.0
    assert c.poller.auth_failure_cooldown_sec == 600.0


def test_load_config_poller_resilience_defaults(tmp_path):
    """Poller resilience settings should have safe defaults."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        """
        device:
          host: 10.0.0.1
          username: u
          password: p
        db:
          url: postgresql://user:pass@localhost:5432/db
        """
    )
    c = load_config(str(cfg))
    assert c.poller.reconnect_delay_sec == 2.0
    assert c.poller.circuit_breaker_threshold == 5
    assert c.poller.circuit_breaker_cooldown_sec == 60.0
    assert c.poller.auth_failure_cooldown_sec == 300.0


def test_gnmi_set_defaults():
    c = GNMIConfig()
    assert c.set_enabled is False
    assert c.set_datastore == "auto"
    assert c.set_auto_refresh is False
    assert c.set_use_lock is False


def test_load_config_gnmi_set_options(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        """
        device:
          host: 10.0.0.1
          username: u
          password: p
        db:
          url: postgresql://user:pass@localhost:5432/db
        gnmi:
          set_enabled: true
          set_datastore: candidate
          set_auto_refresh: true
          set_use_lock: true
        """
    )
    c = load_config(str(cfg))
    assert c.gnmi.set_enabled is True
    assert c.gnmi.set_datastore == "candidate"
    assert c.gnmi.set_auto_refresh is True
    assert c.gnmi.set_use_lock is True


def test_trigger_defaults():
    c = TriggerConfig()
    assert c.enabled is False
    assert c.auto_onboard is True
    assert c.default_interval_sec == 30
    assert c.max_paths_per_request == 50


def test_kafka_defaults():
    c = KafkaConfig()
    assert c.enabled is False
    assert c.publish_poll_updates is True
    assert c.publish_trigger_events is True


def test_load_config_trigger_kafka_options(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        """
        device:
          host: 10.0.0.1
          username: u
          password: p
        db:
          url: postgresql://user:pass@localhost:5432/db
        trigger:
          enabled: true
          auto_onboard: false
          default_interval_sec: 15
          max_paths_per_request: 20
          netconf_timeout_sec: 7.5
        trigger_api:
          enabled: true
          bind: 127.0.0.1
          port: 9912
          bearer_token: abc123
        kafka:
          enabled: true
          brokers: localhost:9092
          topic_prefix: exp
          publish_poll_updates: false
          publish_trigger_events: true
          linger_ms: 5
        """
    )
    c = load_config(str(cfg))
    assert c.trigger.enabled is True
    assert c.trigger.auto_onboard is False
    assert c.trigger.default_interval_sec == 15
    assert c.trigger.max_paths_per_request == 20
    assert c.trigger.netconf_timeout_sec == 7.5
    assert c.trigger_api.enabled is True
    assert c.trigger_api.port == 9912
    assert c.kafka.enabled is True
    assert c.kafka.brokers == "localhost:9092"
    assert c.kafka.publish_poll_updates is False
    assert c.kafka.topic_prefix == "exp"


def test_trigger_api_requires_trigger_enabled(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        """
        device:
          host: 10.0.0.1
          username: u
          password: p
        db:
          url: postgresql://user:pass@localhost:5432/db
        trigger:
          enabled: false
        trigger_api:
          enabled: true
          bearer_token: abc123
        """
    )
    with pytest.raises(ValueError, match="trigger_api.enabled=true requires trigger.enabled=true"):
        load_config(str(cfg))


def test_trigger_api_requires_token(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        """
        device:
          host: 10.0.0.1
          username: u
          password: p
        db:
          url: postgresql://user:pass@localhost:5432/db
        trigger:
          enabled: true
        trigger_api:
          enabled: true
        """
    )
    with pytest.raises(ValueError, match="trigger_api.enabled=true requires trigger_api.bearer_token"):
        load_config(str(cfg))


def test_kafka_enabled_requires_brokers(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        """
        device:
          host: 10.0.0.1
          username: u
          password: p
        db:
          url: postgresql://user:pass@localhost:5432/db
        kafka:
          enabled: true
          brokers: ""
        """
    )
    with pytest.raises(ValueError, match="kafka.enabled=true requires kafka.brokers"):
        load_config(str(cfg))
