import os
import unittest
from unittest.mock import patch

from api.tasks import many_tasks, restart_uwsgi, task1, task2


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
    def test_touches_path_from_env(self) -> None:
        with patch.dict(os.environ, {"UWSGI_RESTART_PATH": "/tmp/api_test_restart.txt"}):
            with patch("api.tasks.many_tasks.Path") as PathMock:
                instance = PathMock.return_value
                restart_uwsgi()
                PathMock.assert_called_once_with("/tmp/api_test_restart.txt")
                instance.touch.assert_called_once()

    def test_falls_back_to_default_path(self) -> None:
        # Clear the env var if set, then verify the default path is used.
        env = {k: v for k, v in os.environ.items() if k != "UWSGI_RESTART_PATH"}
        with patch.dict(os.environ, env, clear=True):
            with patch("api.tasks.many_tasks.Path") as PathMock:
                restart_uwsgi()
                PathMock.assert_called_once_with("/project/workSpace/restart.txt")


if __name__ == "__main__":
    unittest.main()
