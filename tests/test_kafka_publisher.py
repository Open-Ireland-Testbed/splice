from splice.kafka_publisher import KafkaPublisher


class _FakeProducer:
    def __init__(self):
        self.messages = []
        self.flushed = False

    def produce(self, topic, value):
        self.messages.append((topic, value))

    def poll(self, _timeout):
        return None

    def flush(self, timeout=None):
        self.flushed = True
        return 0


def test_kafka_publisher_disabled_noop():
    p = KafkaPublisher(enabled=False)
    assert p.publish_poll_updates("dev-1", [{"path": "/x"}]) == 0
    assert p.publish_trigger_updates("dev-1", "r1", [{"path": "/x"}]) == 0
    p.close()


def test_kafka_publisher_publishes_poll_and_trigger():
    fake = _FakeProducer()
    p = KafkaPublisher(
        enabled=True,
        brokers="unused:9092",
        topic_prefix="splice demo",
        producer=fake,
    )

    n1 = p.publish_poll_updates(
        "roadm 192.0.2.15",
        [{"path": "/system/hostname", "value": "r1", "timestamp_ns": 10}],
    )
    n2 = p.publish_trigger_updates(
        "roadm 192.0.2.15",
        "req-1",
        [{"path": "/system/hostname", "value": "r2", "timestamp_ns": 11}],
    )
    assert n1 == 1
    assert n2 == 1
    assert len(fake.messages) == 2
    assert fake.messages[0][0] == "splice_demo.roadm_192.0.2.15.poll"
    assert fake.messages[1][0] == "splice_demo.roadm_192.0.2.15.trigger"
    assert '"event_type": "poll_update"' in fake.messages[0][1]
    assert '"event_type": "trigger_update"' in fake.messages[1][1]
    p.close()
    assert fake.flushed is True
