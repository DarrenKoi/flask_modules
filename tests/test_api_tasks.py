import unittest
from pathlib import Path
from unittest.mock import patch

from api.tasks import many_tasks
from api.tasks.many_tasks import restart_uwsgi, task1, task2


class TaskSmokeTests(unittest.TestCase):
    def test_task1_returns_none(self) -> None:
        with patch.object(many_tasks, "time") as mock_time:
            mock_time.sleep.return_value = None
            self.assertIsNone(task1())

    def test_task2_returns_none(self) -> None:
        with patch.object(many_tasks, "time") as mock_time:
            mock_time.sleep.return_value = None
            self.assertIsNone(task2())


class RestartUwsgiTests(unittest.TestCase):
    def test_touches_the_configured_restart_path(self) -> None:
        # Patch the module-level name, not an attribute on the Path object:
        # pathlib's classes declare __slots__, so instance attributes are
        # read-only and patch.object(some_path, "touch") raises.
        with patch.object(many_tasks, "UWSGI_RESTART_PATH") as path_mock:
            restart_uwsgi()
        path_mock.touch.assert_called_once_with()

    def test_restart_path_matches_wsgi_touch_reload_target(self) -> None:
        # uWSGI only reloads if this is the exact file wsgi.ini watches:
        # `touch-reload = /project/workSpace/restart.txt`.
        self.assertEqual(
            many_tasks.UWSGI_RESTART_PATH, Path("/project/workSpace/restart.txt")
        )


if __name__ == "__main__":
    unittest.main()
