"""Task callables registered on the scheduler."""

from api.tasks.many_tasks import purge_old_logs, restart_uwsgi, task1, task2

__all__ = ["purge_old_logs", "restart_uwsgi", "task1", "task2"]
