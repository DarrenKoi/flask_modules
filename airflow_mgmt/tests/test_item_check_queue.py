from scripts.item_check_queue import ItemCheckConfig, ItemCheckQueue, run_check_chunk


class FakeRedis:
    def __init__(self) -> None:
        self.values = {}
        self.lists = {}
        self.zsets = {}
        self.sets = {}
        self.hashes = {}

    def set(self, key, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.lists.pop(key, None)
            self.zsets.pop(key, None)
            self.sets.pop(key, None)
            self.hashes.pop(key, None)

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)

    def lpop(self, key, count=None):
        values = self.lists.setdefault(key, [])
        if count is None:
            if not values:
                return None
            return values.pop(0)
        popped = values[:count]
        del values[:count]
        return popped

    def llen(self, key):
        return len(self.lists.get(key, []))

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def zrangebyscore(self, key, minimum, maximum):
        zset = self.zsets.get(key, {})
        return [member for member, score in zset.items() if minimum <= score <= maximum]

    def zrem(self, key, *members):
        zset = self.zsets.setdefault(key, {})
        for member in members:
            zset.pop(member, None)

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    def scard(self, key):
        return len(self.sets.get(key, set()))

    def hset(self, key, field=None, value=None, mapping=None):
        target = self.hashes.setdefault(key, {})
        if mapping is not None:
            target.update(mapping)
        else:
            target[field] = value

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hincrby(self, key, field, amount):
        target = self.hashes.setdefault(key, {})
        target[field] = int(target.get(field, 0)) + amount
        return target[field]

    def hdel(self, key, field):
        self.hashes.setdefault(key, {}).pop(field, None)


def _config(**overrides):
    base = {
        "key_prefix": "test_item_check",
        "batch_size": 3,
        "lease_seconds": 60,
        "max_retries": 2,
    }
    base.update(overrides)
    return ItemCheckConfig(**base)


def test_load_generation_sets_current_generation_and_pending_count():
    client = FakeRedis()
    queue = ItemCheckQueue(client, _config())

    summary = queue.load_generation("gen-1", ["a", "b", "c"])

    assert queue.current_generation() == "gen-1"
    assert summary["pending"] == 3
    assert summary["meta"]["total"] == "3"


def test_claim_respects_batch_size_and_marks_processing():
    client = FakeRedis()
    queue = ItemCheckQueue(client, _config(batch_size=2))
    queue.load_generation("gen-1", ["a", "b", "c"])

    claimed = queue.claim("gen-1")
    summary = queue.summarize("gen-1")

    assert claimed == ["a", "b"]
    assert summary["pending"] == 1
    assert summary["processing"] == 2


def test_run_check_chunk_marks_successful_items_done():
    client = FakeRedis()
    config = _config(batch_size=2)
    queue = ItemCheckQueue(client, config)
    queue.load_generation("gen-1", ["a", "b", "c"])

    result = run_check_chunk(
        0,
        config=config,
        client=client,
        handler=lambda item: {"item": item, "checked": True},
    )
    summary = queue.summarize("gen-1")

    assert result["claimed"] == 2
    assert result["ok"] == 2
    assert summary["done"] == 2
    assert summary["pending"] == 1


def test_run_check_chunk_uses_supplied_generation_snapshot():
    client = FakeRedis()
    config = _config(batch_size=2)
    queue = ItemCheckQueue(client, config)
    queue.load_generation("old-gen", ["old-a", "old-b"])
    queue.load_generation("new-gen", ["new-a", "new-b"])

    result = run_check_chunk(
        0,
        generation="old-gen",
        config=config,
        client=client,
        handler=lambda item: item,
    )

    assert result["generation"] == "old-gen"
    assert queue.summarize("old-gen")["done"] == 2
    assert queue.summarize("new-gen")["pending"] == 2


def test_run_check_chunk_can_skip_without_falling_back_to_current_generation():
    client = FakeRedis()
    config = _config(batch_size=2)
    queue = ItemCheckQueue(client, config)
    queue.load_generation("new-gen", ["new-a", "new-b"])

    result = run_check_chunk(
        0,
        generation=None,
        use_current_generation=False,
        config=config,
        client=client,
        handler=lambda item: item,
    )

    assert result["generation"] is None
    assert result["claimed"] == 0
    assert queue.summarize("new-gen")["pending"] == 2


def test_failed_items_retry_then_move_to_failed():
    client = FakeRedis()
    config = _config(batch_size=1, max_retries=2)
    queue = ItemCheckQueue(client, config)
    queue.load_generation("gen-1", ["a"])

    def fail(_item):
        raise RuntimeError("no data")

    first = run_check_chunk(0, config=config, client=client, handler=fail)
    second = run_check_chunk(0, config=config, client=client, handler=fail)
    summary = queue.summarize("gen-1")

    assert first["retry"] == 1
    assert second["failed"] == 1
    assert summary["failed"] == 1
    assert summary["pending"] == 0


def test_reclaim_expired_items_moves_them_back_to_pending():
    client = FakeRedis()
    config = _config(batch_size=2)
    queue = ItemCheckQueue(client, config)
    queue.load_generation("gen-1", ["a", "b"])
    queue.claim("gen-1")

    reclaimed = queue.reclaim_expired("gen-1", now=9999999999)
    summary = queue.summarize("gen-1")

    assert reclaimed == 2
    assert summary["pending"] == 2
    assert summary["processing"] == 0
