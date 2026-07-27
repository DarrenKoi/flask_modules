import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask

from api import extension, schedule
from api.extension import ApiRedisConfig, TaskLogger
from api.schedule import TOKEN_HEADER


def _build_test_app(test_case, *, lock_held: bool = False, redis_ok: bool = True):
    """Return (app, test_client, lock_client_mock, scheduler_mock).

    Patches the module-level ``schedule.scheduler`` for the lifetime of the
    test (cleanup registered on ``test_case``) so route handlers hit the mock
    too — not just ``init_jobs``.
    """
    template_dir = Path(__file__).resolve().parent.parent / "api" / "templates"
    app = Flask("api_test", template_folder=str(template_dir))
    cfg = ApiRedisConfig()
    app.config["API_REDIS_CONFIG"] = cfg

    lock_client = MagicMock()
    lock_client.set.return_value = not lock_held
    # Default: heartbeat key present, so /health reports overall=ok. Tests that
    # need to simulate a dead scheduler override this with lock_client.get.return_value = None.
    lock_client.get.return_value = "2026-05-12T00:00:00+00:00"
    if not redis_ok:
        lock_client.ping.side_effect = RuntimeError("down")
    # Pipeline context manager for TaskLogger.record() — yields a mock pipe.
    lock_client.pipeline.return_value.__enter__.return_value = MagicMock()
    app.config["LOCK_CLIENT"] = lock_client
    app.config["TASK_LOGGER"] = TaskLogger(lock_client, cfg.log_list_key, cfg.log_list_max)

    scheduler_mock = MagicMock()
    scheduler_mock.running = True
    scheduler_mock.app = app  # mimic flask_apscheduler.APScheduler.init_app
    fake_job = MagicMock()
    fake_job.id = "task1"
    scheduler_mock.get_jobs.return_value = [fake_job]

    patcher = patch.object(schedule, "scheduler", scheduler_mock)
    patcher.start()
    test_case.addCleanup(patcher.stop)

    schedule.init_jobs(app)
    app.register_blueprint(schedule.bp)
    return app, app.test_client(), lock_client, scheduler_mock


class InitJobsTests(unittest.TestCase):
    def test_registers_every_job_in_the_registry(self) -> None:
        # Derived from JOB_FUNCTIONS rather than a hardcoded set: the
        # behavior under test is "init_jobs registers everything in the
        # registry", and the registry itself is config that changes per
        # deployment. A literal set here goes stale the next time a job
        # is added (it did — purge_old_logs).
        app, _, _, scheduler_mock = _build_test_app(self)
        registered = {call.kwargs["id"]: call.kwargs for call in scheduler_mock.add_job.call_args_list}
        user_jobs = set(registered) - {schedule.HEARTBEAT_JOB_ID}
        self.assertEqual(user_jobs, set(schedule.JOB_FUNCTIONS))
        self.assertTrue(registered["task1"]["replace_existing"])

    def test_jobs_registered_by_import_path_not_closure(self) -> None:
        # APScheduler must pickle a stable, importable reference (so a job
        # restored from RedisJobStore goes back through the wrapper). Storing
        # the closure directly would serialize as the underlying task and
        # bypass redis_lock + TaskLogger.
        _, _, _, scheduler_mock = _build_test_app(self)
        registered = {call.kwargs["id"]: call.kwargs for call in scheduler_mock.add_job.call_args_list}
        for name, kwargs in registered.items():
            if name == schedule.HEARTBEAT_JOB_ID:
                continue  # heartbeat uses its own import path; covered separately
            self.assertEqual(kwargs["func"], "api.schedule:run_registered_job")
            self.assertEqual(kwargs["args"], [name])

    def test_heartbeat_job_registered_only_when_scheduler_role(self) -> None:
        _, _, _, scheduler_mock = _build_test_app(self)
        registered = {call.kwargs["id"]: call.kwargs for call in scheduler_mock.add_job.call_args_list}
        self.assertIn(schedule.HEARTBEAT_JOB_ID, registered)
        self.assertEqual(
            registered[schedule.HEARTBEAT_JOB_ID]["func"],
            "api.schedule:write_scheduler_heartbeat",
        )

    def test_register_with_scheduler_false_skips_add_job(self) -> None:
        # Non-scheduler uWSGI workers still build WRAPPED_JOBS (for on-demand
        # dispatch) but must not write to the shared Redis job store.
        template_dir = Path(__file__).resolve().parent.parent / "api" / "templates"
        app = Flask("api_test_noreg", template_folder=str(template_dir))
        cfg = ApiRedisConfig()
        app.config["API_REDIS_CONFIG"] = cfg
        lock_client = MagicMock()
        lock_client.pipeline.return_value.__enter__.return_value = MagicMock()
        app.config["LOCK_CLIENT"] = lock_client
        app.config["TASK_LOGGER"] = TaskLogger(lock_client, cfg.log_list_key, cfg.log_list_max)
        scheduler_mock = MagicMock()
        patcher = patch.object(schedule, "scheduler", scheduler_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

        schedule.init_jobs(app, register_with_scheduler=False)
        scheduler_mock.add_job.assert_not_called()
        # Non-scheduler workers still need every wrapped job for /jobs/run_job.
        self.assertEqual(set(app.config["WRAPPED_JOBS"]), set(schedule.JOB_FUNCTIONS))

    def _ttl_used_by(self, app, lock_client, job: str) -> int:
        """Invoke a wrapped job and report the TTL it passed to SET."""
        lock_client.set.reset_mock()
        lock_client.set.return_value = True
        app.config["WRAPPED_JOBS"][job]()
        return lock_client.set.call_args.kwargs["ex"]

    def test_per_task_lock_ttls_propagate(self) -> None:
        app, _, lock_client, _ = _build_test_app(self)
        self.assertEqual(self._ttl_used_by(app, lock_client, "task1"), 60)
        self.assertEqual(self._ttl_used_by(app, lock_client, "task2"), 60)

    def test_missing_or_null_lock_ttl_falls_back_to_config(self) -> None:
        # Both spellings must reach cfg.lock_ttl. A None leaking through to
        # SET ex=None would create a lock with no expiry at all, so one
        # killed process would block that job permanently.
        app, _, lock_client, _ = _build_test_app(self)
        cfg = app.config["API_REDIS_CONFIG"]
        probe = MagicMock(__name__="ttl_probe")
        base = {"fn": probe, "trigger": IntervalTrigger(seconds=30)}
        for spelling, spec in (
            ("omitted", base),
            ("explicit None", {**base, "lock_ttl": None}),
        ):
            with self.subTest(spelling=spelling):
                with patch.dict(schedule.JOB_FUNCTIONS, {"ttl_probe": spec}):
                    schedule.init_jobs(app, register_with_scheduler=False)
                self.assertEqual(
                    self._ttl_used_by(app, lock_client, "ttl_probe"), cfg.lock_ttl
                )


class JobIdentityTests(unittest.TestCase):
    """The JOB_FUNCTIONS key is the job's identity everywhere. These cases all
    have a registry key that differs from the function's __name__ — the shape
    that any `fn.__name__`-derived identity gets wrong."""

    def _register(self, entries: dict) -> tuple:
        app, _, lock_client, _ = _build_test_app(self)
        with patch.dict(schedule.JOB_FUNCTIONS, entries, clear=True):
            schedule.init_jobs(app, register_with_scheduler=False)
        return app, lock_client

    def test_lock_key_uses_registry_name_not_function_name(self) -> None:
        app, lock_client = self._register(
            {"coverage_hourly": {"fn": MagicMock(__name__="run_coverage"),
                                 "trigger": IntervalTrigger(seconds=30)}}
        )
        lock_client.set.return_value = True
        app.config["WRAPPED_JOBS"]["coverage_hourly"]()
        self.assertEqual(
            lock_client.set.call_args.args[0], "api_skewnono:lock:coverage_hourly"
        )

    def test_log_records_use_registry_name_not_function_name(self) -> None:
        # The dashboard builds its job dropdown from JOB_FUNCTIONS keys, so a
        # record filed under the function name shows up there as "(orphan)".
        app, lock_client = self._register(
            {"coverage_hourly": {"fn": MagicMock(__name__="run_coverage"),
                                 "trigger": IntervalTrigger(seconds=30)}}
        )
        lock_client.set.return_value = True
        pipe = lock_client.pipeline.return_value.__enter__.return_value
        app.config["WRAPPED_JOBS"]["coverage_hourly"]()
        jobs = {json.loads(c.args[1])["job"] for c in pipe.lpush.call_args_list}
        self.assertEqual(jobs, {"coverage_hourly"})

    def test_two_entries_sharing_a_function_get_separate_locks(self) -> None:
        # The latent trap: keyed on fn.__name__ these would share one lock,
        # so the backfill run would skip whenever the hourly one was running.
        shared = MagicMock(__name__="run_coverage")
        app, lock_client = self._register(
            {
                "coverage_hourly": {"fn": shared, "trigger": IntervalTrigger(seconds=30)},
                "coverage_backfill": {"fn": shared, "trigger": IntervalTrigger(seconds=30)},
            }
        )
        lock_client.set.return_value = True
        for job in ("coverage_hourly", "coverage_backfill"):
            app.config["WRAPPED_JOBS"][job]()
        keys = [c.args[0] for c in lock_client.set.call_args_list]
        self.assertEqual(
            keys,
            ["api_skewnono:lock:coverage_hourly", "api_skewnono:lock:coverage_backfill"],
        )


class SkipRecordTests(unittest.TestCase):
    """A skipped run records the holder as structured fields. The dashboard's
    detailFor() renders them, so held_since goes through toKst like every
    other timestamp instead of being pre-formatted here as raw UTC."""

    def _skip_record(self, raw_owner: object, ttl: int) -> dict:
        app, _, lock_client, _ = _build_test_app(self, lock_held=True)
        pipe = lock_client.pipeline.return_value.__enter__.return_value
        pipe.execute.return_value = [raw_owner, ttl]
        app.config["WRAPPED_JOBS"]["task1"]()
        return json.loads(pipe.lpush.call_args.args[1])

    def test_carries_holder_identity_and_remaining_ttl(self) -> None:
        rec = self._skip_record(
            json.dumps(
                {"host": "web-3", "pid": 812, "acquired": "2026-07-27T05:00:00+00:00"}
            ),
            47,
        )
        self.assertEqual(rec["event"], "skip")
        self.assertEqual(rec["job"], "task1")
        self.assertEqual(rec["holder"], "web-3:812")
        self.assertEqual(rec["held_since"], "2026-07-27T05:00:00+00:00")
        self.assertEqual(rec["ttl_remaining"], 47)

    def test_records_skip_even_when_holder_is_unreadable(self) -> None:
        # A lock written by a pre-upgrade deploy holds a bare uuid hex: no
        # holder fields, but the skip itself must still reach the dashboard.
        rec = self._skip_record("deadbeef", 9)
        self.assertEqual(rec["event"], "skip")
        self.assertEqual(rec["ttl_remaining"], 9)
        self.assertNotIn("holder", rec)


class HealthRouteTests(unittest.TestCase):
    def test_healthy(self) -> None:
        _, client, _, _ = _build_test_app(self)
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["redis"], "ok")
        self.assertTrue(body["scheduler"]["running"])
        self.assertIn("task1", body["scheduler"]["jobs"])

    def test_degraded_when_redis_down(self) -> None:
        _, client, _, _ = _build_test_app(self, redis_ok=False)
        body = client.get("/health").get_json()
        self.assertEqual(body["status"], "degraded")
        self.assertTrue(body["redis"].startswith("error:"))


class JobsLogsRouteTests(unittest.TestCase):
    def test_returns_parsed_records(self) -> None:
        _, client, lock_client, _ = _build_test_app(self)
        lock_client.lrange.return_value = [
            json.dumps({"job": "task1", "event": "end", "duration_ms": 1003}),
        ]
        body = client.get("/jobs/logs?limit=10").get_json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["job"], "task1")
        # limit is capped to 10 (way under cfg.log_list_max=500)
        lock_client.lrange.assert_called_once_with("api_skewnono:logs:tasks", 0, 9)

    def test_caps_limit_at_log_list_max(self) -> None:
        _, client, lock_client, _ = _build_test_app(self)
        lock_client.lrange.return_value = []
        client.get("/jobs/logs?limit=99999")
        # cfg.log_list_max=500 → range index = 499
        lock_client.lrange.assert_called_once_with("api_skewnono:logs:tasks", 0, 499)


class RunJobRouteTests(unittest.TestCase):
    """The /jobs/run_job route is shared-secret authenticated. Manual dispatch
    is disabled outright when ``schedule.SCHEDULE_TOKEN`` is empty.
    """

    AUTH = {TOKEN_HEADER: "secret"}

    def _app_with_token(self, **kwargs):
        patcher = patch.object(schedule, "SCHEDULE_TOKEN", "secret")
        patcher.start()
        self.addCleanup(patcher.stop)
        return _build_test_app(self, **kwargs)

    def test_missing_header_returns_401(self) -> None:
        _, client, _, _ = self._app_with_token()
        resp = client.post("/jobs/run_job", json={"job_name": "task1"})
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_returns_401(self) -> None:
        _, client, _, _ = self._app_with_token()
        resp = client.post(
            "/jobs/run_job",
            json={"job_name": "task1"},
            headers={TOKEN_HEADER: "nope"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_disabled_when_token_empty(self) -> None:
        _, client, _, _ = _build_test_app(self)
        resp = client.post(
            "/jobs/run_job",
            json={"job_name": "task1"},
            headers={TOKEN_HEADER: "anything"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_restart_uwsgi_blocked_from_manual_dispatch(self) -> None:
        _, client, _, _ = self._app_with_token()
        resp = client.post(
            "/jobs/run_job",
            json={"job_name": "restart_uwsgi"},
            headers=self.AUTH,
        )
        self.assertEqual(resp.status_code, 403)

    def test_unknown_job_returns_404(self) -> None:
        _, client, _, _ = self._app_with_token()
        resp = client.post("/jobs/run_job", json={"job_name": "nope"}, headers=self.AUTH)
        self.assertEqual(resp.status_code, 404)

    def test_known_job_invokes_wrapped_fn(self) -> None:
        app, client, _, _ = self._app_with_token()
        with patch.dict(app.config["WRAPPED_JOBS"], {"task1": MagicMock()}):
            resp = client.post("/jobs/run_job", json={"job_name": "task1"}, headers=self.AUTH)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json(), {"status": "ok", "job": "task1"})
            app.config["WRAPPED_JOBS"]["task1"].assert_called_once()

    def test_lock_held_skips_real_work_but_returns_200(self) -> None:
        _, client, lock_client, _ = self._app_with_token(lock_held=True)
        # SET returns False → skip; no release fires (we never acquired).
        resp = client.post("/jobs/run_job", json={"job_name": "task1"}, headers=self.AUTH)
        self.assertEqual(resp.status_code, 200)
        lock_client.set.assert_called()
        lock_client.register_script.return_value.assert_not_called()


class RunRegisteredJobTests(unittest.TestCase):
    """The pickle-safe entry point APScheduler stores by import path.

    The real scheduler thread fires this with NO Flask context (verified
    against flask_apscheduler 1.x: it just calls ``job.func(*args, **kwargs)``
    directly). These tests therefore deliberately do NOT push an app context
    — they exercise the runner's own ``scheduler.app.app_context()`` push.
    """

    def test_looks_up_and_invokes_wrapped_callable(self) -> None:
        app, _, _, scheduler_mock = _build_test_app(self)
        # scheduler_mock.app is already bound by _build_test_app.
        sentinel = MagicMock(return_value="done")
        app.config["WRAPPED_JOBS"] = {"task1": sentinel}
        result = schedule.run_registered_job("task1")  # no with-block on purpose
        self.assertEqual(result, "done")
        sentinel.assert_called_once_with()

    def test_unknown_job_raises_keyerror(self) -> None:
        app, _, _, _ = _build_test_app(self)
        app.config["WRAPPED_JOBS"] = {}
        with self.assertRaises(KeyError):
            schedule.run_registered_job("missing")

    def test_raises_when_scheduler_has_no_app(self) -> None:
        _, _, _, scheduler_mock = _build_test_app(self)
        scheduler_mock.app = None
        with self.assertRaises(RuntimeError):
            schedule.run_registered_job("task1")


class WriteSchedulerHeartbeatTests(unittest.TestCase):
    def test_sets_heartbeat_key_with_ttl(self) -> None:
        app, _, lock_client, _ = _build_test_app(self)
        schedule.write_scheduler_heartbeat()
        lock_client.set.assert_called_once()
        args, kwargs = lock_client.set.call_args
        cfg = app.config["API_REDIS_CONFIG"]
        self.assertEqual(args[0], cfg.heartbeat_key)
        self.assertEqual(kwargs["ex"], cfg.heartbeat_ttl)

    def test_no_op_when_scheduler_has_no_app(self) -> None:
        _, _, lock_client, scheduler_mock = _build_test_app(self)
        scheduler_mock.app = None
        schedule.write_scheduler_heartbeat()  # must not raise
        lock_client.set.assert_not_called()


class HealthHeartbeatTests(unittest.TestCase):
    """Whole-service health depends on the shared Redis heartbeat, not on
    whether the local worker happens to own the scheduler thread."""

    def test_worker_role_ok_when_heartbeat_fresh(self) -> None:
        # Non-scheduler worker, no local scheduler running, but a fresh
        # heartbeat exists in Redis → service is healthy.
        app, client, _, scheduler_mock = _build_test_app(self)
        scheduler_mock.running = False
        scheduler_mock.get_jobs.return_value = []
        app.config["IS_SCHEDULER_WORKER"] = False
        body = client.get("/health").get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["scheduler"]["role"], "worker")
        self.assertIsNotNone(body["scheduler"]["heartbeat"])

    def test_worker_role_degrades_when_heartbeat_missing(self) -> None:
        # The reviewer's scenario: request-only worker is locally fine but
        # the scheduler worker is dead → heartbeat key has expired → degraded.
        app, client, lock_client, scheduler_mock = _build_test_app(self)
        scheduler_mock.running = False
        scheduler_mock.get_jobs.return_value = []
        app.config["IS_SCHEDULER_WORKER"] = False
        lock_client.get.return_value = None  # key expired
        body = client.get("/health").get_json()
        self.assertEqual(body["status"], "degraded")
        self.assertIsNone(body["scheduler"]["heartbeat"])

    def test_scheduler_role_degrades_when_heartbeat_missing(self) -> None:
        app, client, lock_client, _ = _build_test_app(self)
        app.config["IS_SCHEDULER_WORKER"] = True
        lock_client.get.return_value = None
        body = client.get("/health").get_json()
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["scheduler"]["role"], "scheduler")


class DashboardRouteTests(unittest.TestCase):
    def test_renders_template_with_job_list(self) -> None:
        _, client, _, _ = _build_test_app(self)
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertIn("API scheduler", body)
        self.assertIn("task1", body)
        self.assertIn("task2", body)
        self.assertIn("restart_uwsgi", body)


class OptionalJobKwargsTests(unittest.TestCase):
    def test_optional_scheduler_kwargs_reach_add_job(self):
        # A 15s job needs its own misfire window and executor. Without the
        # pass-through, SCHEDULER_JOB_DEFAULTS' 60s misfire applies to every
        # job, far too long for a job firing every 15 seconds.
        app, _client, _lock, scheduler_mock = _build_test_app(self)
        probe = {
            "fn": lambda: None,
            "trigger": IntervalTrigger(seconds=15),
            "lock_ttl": 45,
            "manual_dispatch": True,
            "misfire_grace_time": 10,
            "executor": "fast",
        }
        with patch.dict(schedule.JOB_FUNCTIONS, {"probe": probe}):
            scheduler_mock.add_job.reset_mock()
            schedule.init_jobs(app)
        calls = {c.kwargs["id"]: c.kwargs for c in scheduler_mock.add_job.call_args_list}
        self.assertEqual(calls["probe"]["misfire_grace_time"], 10)
        self.assertEqual(calls["probe"]["executor"], "fast")

    def test_existing_jobs_pass_no_optional_kwargs(self):
        # Backward compatibility: jobs without the keys must be unchanged.
        app, _client, _lock, scheduler_mock = _build_test_app(self)
        scheduler_mock.add_job.reset_mock()
        schedule.init_jobs(app)
        calls = {c.kwargs["id"]: c.kwargs for c in scheduler_mock.add_job.call_args_list}
        self.assertNotIn("misfire_grace_time", calls["task1"])
        self.assertNotIn("executor", calls["task1"])

    def test_fast_executor_is_registered(self):
        app = Flask("api_test")
        cfg = ApiRedisConfig()
        extension.configure_scheduler(app, cfg)
        self.assertIn("fast", app.config["SCHEDULER_EXECUTORS"])
        self.assertIn("default", app.config["SCHEDULER_EXECUTORS"])


if __name__ == "__main__":
    unittest.main()
