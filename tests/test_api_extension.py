import json
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from api import extension
from api.extension import (
    ApiRedisConfig,
    TaskLogger,
    describe_lock_holder,
    lock_owner_token,
    read_task_logs,
    redis_lock,
    utc_stamp,
)


def make_lock_client(**attrs: object) -> MagicMock:
    """Mock Redis client that redis-py's ``Lock`` can actually drive.

    ``Lock.acquire`` runs the token through ``client.get_encoder()``, so the
    mock needs a real ``Encoder`` — otherwise the token reaching SET is a
    MagicMock and nothing about the stored value is observable.
    """
    from redis.connection import Encoder

    client = MagicMock()
    client.get_encoder.return_value = Encoder("utf-8", "strict", False)
    client.pipeline.return_value.__enter__.return_value = MagicMock()
    for key, value in attrs.items():
        setattr(client, key, value)
    return client


def reset_lock_scripts() -> None:
    """Un-cache redis-py's Lua scripts, which live on the ``Lock`` CLASS.

    ``Lock.register_scripts`` only registers when the class attribute is
    still None, so without this the first mock client in a session stays
    bound for every later test and their script assertions read a stale mock.
    """
    from redis.lock import Lock

    Lock.lua_release = Lock.lua_extend = Lock.lua_reacquire = None


def _make_logger(client: MagicMock | None = None) -> tuple[TaskLogger, MagicMock, MagicMock]:
    """Build a TaskLogger backed by a mock client + mock pipeline."""
    client = client if client is not None else make_lock_client()
    pipe = MagicMock()
    client.pipeline.return_value.__enter__.return_value = pipe
    return TaskLogger(client, "LK", 3), client, pipe


class ApiRedisConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = ApiRedisConfig()
        self.assertEqual(cfg.host, "10.156.133.129")
        self.assertEqual(cfg.port, 10108)
        self.assertEqual(cfg.lock_ttl, 300)
        self.assertEqual(cfg.db, 0)
        self.assertEqual(cfg.jobstore_key_prefix, "api_skewnono:jobs:")
        self.assertEqual(cfg.lock_key_prefix, "api_skewnono:lock:")
        self.assertEqual(cfg.log_list_key, "api_skewnono:logs:tasks")
        self.assertEqual(cfg.log_list_max, 500)

    def test_overrides_via_constructor(self) -> None:
        cfg = ApiRedisConfig(
            host="redis.internal",
            port=6380,
            db=2,
            lock_ttl=900,
            log_list_max=100,
        )
        self.assertEqual(cfg.host, "redis.internal")
        self.assertEqual(cfg.port, 6380)
        self.assertEqual(cfg.db, 2)
        self.assertEqual(cfg.lock_ttl, 900)
        self.assertEqual(cfg.log_list_max, 100)

    def test_to_client_kwargs_shares_one_db_with_the_jobstore(self) -> None:
        # Locks, logs and heartbeat live in the same db as the job store —
        # the key prefixes keep them apart, so nothing depends on Redis
        # supporting more than db 0.
        cfg = ApiRedisConfig(host="h", port=1, db=7, password="pw")
        kwargs = cfg.to_client_kwargs(decode_responses=True)
        self.assertEqual(kwargs["db"], 7)
        self.assertEqual(kwargs["password"], "pw")
        self.assertTrue(kwargs["decode_responses"])


class RedisLockTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_lock_scripts()
        self.client = make_lock_client()
        # Populated by Lock.register_scripts() on first construction.
        self.scripts = self.client.register_script.return_value

    def _release_calls(self) -> list:
        return [c for c in self.scripts.call_args_list if c.kwargs.get("keys") == ["k"]]

    def test_runs_wrapped_fn_when_lock_acquired(self) -> None:
        self.client.set.return_value = True
        inner = MagicMock(return_value="result")
        wrapped = redis_lock(self.client, key="k", ttl=30)(inner)
        self.assertEqual(wrapped("a", b=1), "result")
        args, kwargs = self.client.set.call_args
        self.assertEqual(args[0], "k")
        self.assertTrue(kwargs.get("nx"))
        # Lock uses px (milliseconds), not ex (seconds).
        self.assertEqual(kwargs.get("px"), 30000)
        token = args[1]
        # The stored value is our owner payload, encoded to bytes by redis-py.
        self.assertEqual(json.loads(token)["pid"], extension.os.getpid())
        inner.assert_called_once_with("a", b=1)
        self.client.delete.assert_not_called()
        self.scripts.assert_called_once_with(keys=["k"], args=[token], client=self.client)

    def test_skips_when_lock_held(self) -> None:
        self.client.set.return_value = None  # SET NX returns nil when held
        inner = MagicMock()
        inner.__name__ = "task_under_lock"
        wrapped = redis_lock(self.client, key="k", ttl=30)(inner)
        self.assertIsNone(wrapped())
        inner.assert_not_called()
        self.scripts.assert_not_called()

    def test_invokes_on_skip_callback_with_holder_info(self) -> None:
        self.client.set.return_value = None
        pipe = self.client.pipeline.return_value.__enter__.return_value
        pipe.execute.return_value = [
            json.dumps({"host": "web-2", "pid": 41, "acquired": "2026-07-27T05:00:00+00:00"}),
            120,
        ]
        on_skip = MagicMock()
        inner = MagicMock()
        inner.__name__ = "the_task"
        wrapped = redis_lock(self.client, key="k", ttl=30, on_skip=on_skip)(inner)
        wrapped()
        # Holder info only — redis_lock does not name the caller's job.
        (info,) = on_skip.call_args.args
        self.assertEqual(info["holder"], "web-2:41")
        self.assertEqual(info["ttl_remaining"], 120)

    def test_releases_lock_on_exception(self) -> None:
        self.client.set.return_value = True
        inner = MagicMock(side_effect=RuntimeError("boom"))
        wrapped = redis_lock(self.client, key="k", ttl=30)(inner)
        with self.assertRaises(RuntimeError):
            wrapped()
        self.scripts.assert_called_once()

    def test_swallows_lost_ownership_on_release(self) -> None:
        # The key expired mid-run and someone else owns it: redis-py raises
        # LockNotOwnedError. There is nothing of ours to delete, and the task
        # already succeeded — it must not surface as a job failure.
        self.client.set.return_value = True
        self.scripts.return_value = 0  # CAS says: not yours
        wrapped = redis_lock(self.client, key="k", ttl=30)(MagicMock(return_value="ok"))
        self.assertEqual(wrapped(), "ok")

    def test_watchdog_thread_can_extend_the_lock(self) -> None:
        # Guards thread_local=False. redis-py's default stashes the
        # acquisition token in threading.local(), so the renewal thread would
        # find none and extend() would raise LockError on every tick — the
        # lock would then quietly expire mid-run instead of being renewed.
        self.client.set.return_value = True
        captured: dict = {}
        errors: list[Exception] = []

        def fake_start(lock, ttl):
            captured["lock"] = lock
            return threading.Event()

        def job() -> None:
            def tick() -> None:
                try:
                    captured["lock"].extend(30, replace_ttl=True)
                except Exception as exc:  # noqa: BLE001 - recorded, then asserted
                    errors.append(exc)

            thread = threading.Thread(target=tick)
            thread.start()
            thread.join()

        with patch.object(extension, "_start_renewal", fake_start):
            redis_lock(self.client, key="k", ttl=30)(job)()

        self.assertEqual(errors, [])

    def test_unique_token_per_acquisition(self) -> None:
        # Two acquisitions must mint different tokens so a stale wrapper
        # can never release a newer owner's key.
        self.client.set.return_value = True
        inner = MagicMock()
        wrapped = redis_lock(self.client, key="k", ttl=30)(inner)
        wrapped()
        wrapped()
        tokens = [c.args[1] for c in self.client.set.call_args_list]
        self.assertEqual(len(set(tokens)), 2)

    def test_starts_daemon_renewal_thread_and_stops_it_before_release(self) -> None:
        self.client.set.return_value = True
        order: list[str] = []
        self.scripts.side_effect = lambda **kw: order.append("release") or 1

        with patch.object(extension.threading, "Thread") as mock_thread:
            mock_thread.return_value.start.side_effect = lambda: order.append("start")
            wrapped = redis_lock(self.client, key="k", ttl=30)(MagicMock())
            wrapped()

        kwargs = mock_thread.call_args.kwargs
        self.assertTrue(kwargs["daemon"])
        self.assertEqual(kwargs["target"], extension._renew_until_stopped)
        lock, ttl, stop = kwargs["args"]
        self.assertEqual(lock.name, "k")
        self.assertEqual(ttl, 30)
        # The watchdog must be told to stop BEFORE the key is deleted,
        # otherwise it could re-EXPIRE a lock that no longer has an owner.
        self.assertTrue(stop.is_set())
        self.assertEqual(order, ["start", "release"])


class UtcStampTests(unittest.TestCase):
    def test_is_second_precision_aware_utc(self) -> None:
        # Aware UTC, never naive: the dashboard's toKst() needs the offset to
        # convert, and a naive string would render 9h off in Seoul.
        parsed = datetime.fromisoformat(utc_stamp())
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.microsecond, 0)


class LockOwnerTokenTests(unittest.TestCase):
    def test_encodes_identity_and_is_unique(self) -> None:
        first = lock_owner_token()
        second = lock_owner_token()
        self.assertNotEqual(first, second)
        rec = json.loads(first)
        self.assertEqual(rec["pid"], extension.os.getpid())
        self.assertIn("host", rec)
        self.assertIn("acquired", rec)


class DescribeLockHolderTests(unittest.TestCase):
    def _client_returning(self, raw: object, ttl: int) -> MagicMock:
        client = MagicMock()
        pipe = MagicMock()
        client.pipeline.return_value.__enter__.return_value = pipe
        pipe.execute.return_value = [raw, ttl]
        return client

    def test_parses_owner_payload(self) -> None:
        client = self._client_returning(
            json.dumps({"host": "web-1", "pid": 7, "acquired": "2026-07-27T05:00:00+00:00"}),
            42,
        )
        info = describe_lock_holder(client, "k")
        self.assertEqual(info["holder"], "web-1:7")
        self.assertEqual(info["held_since"], "2026-07-27T05:00:00+00:00")
        self.assertEqual(info["ttl_remaining"], 42)

    def test_reports_ttl_only_when_value_is_unparseable(self) -> None:
        # Locks written by a pre-upgrade deploy hold a bare uuid hex.
        info = describe_lock_holder(self._client_returning("deadbeef", 9), "k")
        self.assertEqual(info, {"ttl_remaining": 9})

    def test_returns_empty_when_redis_unreachable(self) -> None:
        # A skip record must still be written even if the holder read fails.
        client = MagicMock()
        client.pipeline.side_effect = RuntimeError("redis down")
        self.assertEqual(describe_lock_holder(client, "k"), {})


class RenewUntilStoppedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = MagicMock(name="lock")
        self.lock.name = "k"

    def _stop_after(self, ticks: int) -> MagicMock:
        """A stop event that lets ``ticks`` renewals through, then halts."""
        stop = MagicMock(spec=threading.Event)
        stop.wait.side_effect = [False] * ticks + [True]
        return stop

    def test_rearms_ttl_each_tick_until_stopped(self) -> None:
        extension._renew_until_stopped(self.lock, 30, self._stop_after(2))
        self.assertEqual(self.lock.extend.call_count, 2)
        # replace_ttl=True is load-bearing: the default ADDS to the remaining
        # ttl, so each tick would push the expiry further out and a killed
        # process would strand the lock far beyond ttl.
        self.lock.extend.assert_called_with(30, replace_ttl=True)

    def test_renews_at_one_third_of_ttl(self) -> None:
        stop = self._stop_after(1)
        extension._renew_until_stopped(self.lock, 300, stop)
        stop.wait.assert_called_with(100)

    def test_renew_interval_never_drops_below_one_second(self) -> None:
        stop = self._stop_after(1)
        extension._renew_until_stopped(self.lock, 2, stop)
        stop.wait.assert_called_with(1)

    def test_stops_when_ownership_lost(self) -> None:
        # The key expired and someone else owns it now; redis-py raises
        # LockNotOwnedError. Renewing again would extend a stranger's lock.
        from redis.exceptions import LockNotOwnedError

        self.lock.extend.side_effect = LockNotOwnedError("gone")
        extension._renew_until_stopped(self.lock, 30, self._stop_after(5))
        self.assertEqual(self.lock.extend.call_count, 1)

    def test_keeps_renewing_after_a_transient_redis_error(self) -> None:
        self.lock.extend.side_effect = [RuntimeError("blip"), True]
        extension._renew_until_stopped(self.lock, 30, self._stop_after(2))
        self.assertEqual(self.lock.extend.call_count, 2)


class TaskLoggerRecordTests(unittest.TestCase):
    def test_pipelines_lpush_and_ltrim(self) -> None:
        logger, client, pipe = _make_logger()
        logger.record("task1", "skip", message="lock held")
        client.pipeline.assert_called_once()
        pipe.lpush.assert_called_once()
        key, payload = pipe.lpush.call_args.args
        self.assertEqual(key, "LK")
        rec = json.loads(payload)
        self.assertEqual(rec["job"], "task1")
        self.assertEqual(rec["event"], "skip")
        self.assertEqual(rec["message"], "lock held")
        pipe.ltrim.assert_called_once_with("LK", 0, 2)
        pipe.execute.assert_called_once()

    def test_swallows_pipeline_failure(self) -> None:
        client = MagicMock()
        client.pipeline.side_effect = RuntimeError("redis down")
        logger = TaskLogger(client, "LK", 5)
        logger.record("task1", "start")  # must not raise


class TaskLoggerWrapTests(unittest.TestCase):
    def test_emits_start_then_end_on_success(self) -> None:
        logger, client, pipe = _make_logger()

        def task() -> str:
            return "ok"

        logger.wrap(task)()
        events = [json.loads(c.args[1])["event"] for c in pipe.lpush.call_args_list]
        self.assertEqual(events, ["start", "end"])
        last = json.loads(pipe.lpush.call_args_list[-1].args[1])
        self.assertIn("duration_ms", last)

    def test_emits_start_then_error_on_exception(self) -> None:
        logger, client, pipe = _make_logger()

        def task() -> None:
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            logger.wrap(task)()
        events = [json.loads(c.args[1])["event"] for c in pipe.lpush.call_args_list]
        self.assertEqual(events, ["start", "error"])
        last = json.loads(pipe.lpush.call_args_list[-1].args[1])
        self.assertIn("nope", last["error"])
        self.assertIn("duration_ms", last)


class ComposedLockAndLogTests(unittest.TestCase):
    """The wrapping order in schedule.init_jobs matters: a held lock must emit
    only a single ``skip`` record — not ``start``, ``skip``, ``end``."""

    def test_skip_emits_only_skip(self) -> None:
        logger, client, pipe = _make_logger()
        client.set.return_value = False  # lock already held
        pipe.execute.return_value = [None, 5]

        def task() -> None:
            return None

        wrapped = redis_lock(
            client,
            key="k",
            ttl=30,
            on_skip=lambda info: logger.record("task", "skip", **info),
        )(logger.wrap(task))
        wrapped()

        events = [json.loads(c.args[1])["event"] for c in pipe.lpush.call_args_list]
        self.assertEqual(events, ["skip"])


class ReadTaskLogsTests(unittest.TestCase):
    def test_parses_and_skips_malformed(self) -> None:
        client = MagicMock()
        client.lrange.return_value = [
            json.dumps({"job": "task1", "event": "end"}),
            "not-json",
            json.dumps({"job": "task2", "event": "start"}),
        ]
        records = read_task_logs(client, "LK", limit=5)
        client.lrange.assert_called_once_with("LK", 0, 4)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["job"], "task1")
        self.assertEqual(records[1]["job"], "task2")


class ConfigureSchedulerTests(unittest.TestCase):
    def test_sets_expected_flask_config_keys(self) -> None:
        from flask import Flask

        app = Flask(__name__)
        cfg = ApiRedisConfig()
        with patch("apscheduler.jobstores.redis.RedisJobStore") as mock_store:
            extension.configure_scheduler(app, cfg)
        self.assertIn("default", app.config["SCHEDULER_JOBSTORES"])
        self.assertEqual(app.config["SCHEDULER_TIMEZONE"], "Asia/Seoul")
        self.assertFalse(app.config["SCHEDULER_API_ENABLED"])
        self.assertEqual(app.config["SCHEDULER_JOB_DEFAULTS"]["max_instances"], 1)
        kwargs = mock_store.call_args.kwargs
        self.assertEqual(kwargs["jobs_key"], "api_skewnono:jobs:jobs")
        self.assertEqual(kwargs["run_times_key"], "api_skewnono:jobs:run_times")

    def test_forwards_ssl_and_timeout_and_extras_to_jobstore(self) -> None:
        from flask import Flask

        app = Flask(__name__)
        cfg = ApiRedisConfig(
            ssl=True,
            socket_timeout=5.0,
            password="pw",
            extra_client_kwargs={"socket_keepalive": True},
        )
        with patch("apscheduler.jobstores.redis.RedisJobStore") as mock_store:
            extension.configure_scheduler(app, cfg)
        kwargs = mock_store.call_args.kwargs
        self.assertTrue(kwargs["ssl"])
        self.assertEqual(kwargs["socket_timeout"], 5.0)
        self.assertEqual(kwargs["password"], "pw")
        self.assertTrue(kwargs["socket_keepalive"])


if __name__ == "__main__":
    unittest.main()
