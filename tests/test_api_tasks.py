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
    def test_touches_hardcoded_restart_path(self) -> None:
        with patch.object(many_tasks.UWSGI_RESTART_PATH, "touch") as touch_mock:
            restart_uwsgi()
            touch_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
