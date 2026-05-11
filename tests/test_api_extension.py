import json
import os
import unittest
from unittest.mock import MagicMock, patch

from api import extension
from api.extension import (
    ApiRedisConfig,
    TaskLogger,
    read_task_logs,
    redis_lock,
)


def _reset_release_script_cache() -> None:
    """The module caches a single ``Script`` instance across calls; reset it
    between tests so each test sees a fresh ``register_script`` call on its
    own mock client."""
    extension._release_lock = None


def _make_logger(client: MagicMock | None = None) -> tuple[TaskLogger, MagicMock, MagicMock]:
    """Build a TaskLogger backed by a mock client + mock pipeline."""
    client = client or MagicMock()
    pipe = MagicMock()
    client.pipeline.return_value.__enter__.return_value = pipe
    return TaskLogger(client, "LK", 3), client, pipe


class ApiRedisConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = ApiRedisConfig()
        self.assertEqual(cfg.host, "localhost")
        self.assertEqual(cfg.lock_ttl, 1200)
        self.assertEqual(cfg.lock_db, 1)
        self.assertEqual(cfg.jobstore_key_prefix, "api_skewnono:jobs:")
        self.assertEqual(cfg.lock_key_prefix, "api_skewnono:lock:")
        self.assertEqual(cfg.log_list_key, "api_skewnono:logs:tasks")
        self.assertEqual(cfg.log_list_max, 500)

    def test_from_env_overrides(self) -> None:
        env = {
            "API_REDIS_HOST": "redis.internal",
            "API_REDIS_PORT": "6380",
            "API_REDIS_DB": "2",
            "API_REDIS_LOCK_DB": "3",
            "API_REDIS_LOCK_TTL": "900",
            "API_REDIS_LOG_MAX": "100",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = ApiRedisConfig.from_env()
        self.assertEqual(cfg.host, "redis.internal")
        self.assertEqual(cfg.port, 6380)
        self.assertEqual(cfg.db, 2)
        self.assertEqual(cfg.lock_db, 3)
        self.assertEqual(cfg.lock_ttl, 900)
        self.assertEqual(cfg.log_list_max, 100)

    def test_to_lock_client_kwargs_uses_lock_db(self) -> None:
        cfg = ApiRedisConfig(host="h", port=1, db=0, lock_db=7, password="pw")
        kwargs = cfg.to_lock_client_kwargs()
        self.assertEqual(kwargs["db"], 7)
        self.assertEqual(kwargs["password"], "pw")
        self.assertTrue(kwargs["decode_responses"])


class RedisLockTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_release_script_cache()
        self.client = MagicMock()
        self.release = self.client.register_script.return_value

    def test_runs_wrapped_fn_when_lock_acquired(self) -> None:
        self.client.set.return_value = True
        inner = MagicMock(return_value="result")
        wrapped = redis_lock(self.client, key="k", ttl=30)(inner)
        self.assertEqual(wrapped("a", b=1), "result")
        args, kwargs = self.client.set.call_args
        self.assertEqual(args[0], "k")
        self.assertIsInstance(args[1], str)
        self.assertTrue(kwargs.get("nx"))
        self.assertEqual(kwargs.get("ex"), 30)
        token = args[1]
        inner.assert_called_once_with("a", b=1)
        self.client.delete.assert_not_called()
        self.release.assert_called_once_with(keys=["k"], args=[token], client=self.client)

    def test_skips_when_lock_held(self) -> None:
        self.client.set.return_value = False
        inner = MagicMock()
        inner.__name__ = "task_under_lock"
        wrapped = redis_lock(self.client, key="k", ttl=30)(inner)
        self.assertIsNone(wrapped())
        inner.assert_not_called()
        self.release.assert_not_called()

    def test_invokes_on_skip_callback_when_lock_held(self) -> None:
        self.client.set.return_value = False
        on_skip = MagicMock()
        inner = MagicMock()
        inner.__name__ = "the_task"
        wrapped = redis_lock(self.client, key="k", ttl=30, on_skip=on_skip)(inner)
        wrapped()
        on_skip.assert_called_once_with("the_task")

    def test_releases_lock_on_exception(self) -> None:
        self.client.set.return_value = True
        inner = MagicMock(side_effect=RuntimeError("boom"))
        wrapped = redis_lock(self.client, key="k", ttl=30)(inner)
        with self.assertRaises(RuntimeError):
            wrapped()
        self.release.assert_called_once()

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

    def test_release_script_is_cached_across_decorators(self) -> None:
        # register_script should only be called once at module level — the
        # cached Script is reused by every redis_lock(...) invocation.
        redis_lock(self.client, key="a", ttl=30)(MagicMock())
        redis_lock(self.client, key="b", ttl=30)(MagicMock())
        self.client.register_script.assert_called_once()


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
    """The wrapping order in schedule._wrap matters: a held lock must emit only
    a single ``skip`` record — not ``start``, ``skip``, ``end``."""

    def setUp(self) -> None:
        _reset_release_script_cache()

    def test_skip_emits_only_skip(self) -> None:
        logger, client, pipe = _make_logger()
        client.set.return_value = False  # lock already held

        def task() -> None:
            return None

        wrapped = redis_lock(
            client,
            key="k",
            ttl=30,
            on_skip=lambda name: logger.record(name, "skip", message="lock held"),
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
