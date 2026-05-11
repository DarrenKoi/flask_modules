"""Task callables registered on the scheduler."""

from api.tasks.many_tasks import restart_uwsgi, task1, task2

__all__ = ["restart_uwsgi", "task1", "task2"]
