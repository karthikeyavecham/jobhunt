"""In-process background jobs for work too slow to hold a request open.

A full cycle can run eight minutes and a document takes about a minute — far
past what a browser will wait for. The Slack path already acks first and works
on a thread; this gives the web UI the same shape, plus somewhere to show what
is currently running so "started" does not mean "vanished".

Deliberately not Celery or RQ: this is one user on a Raspberry Pi, and a broker
would be more moving parts than the whole app. State is in memory, so a restart
forgets running tasks — acceptable, because every task here is idempotent or
safely repeatable, and the database is the real record of what happened.
"""

import logging
import threading
import time
from collections import OrderedDict

log = logging.getLogger(__name__)

#: Completed tasks kept for the activity panel.
HISTORY_LIMIT = 12


class Task(object):
    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.started_at = time.time()
        self.finished_at = None
        self.result = None      # short human-readable summary
        self.error = None

    @property
    def running(self):
        return self.finished_at is None

    @property
    def seconds(self):
        return (self.finished_at or time.time()) - self.started_at


class Registry(object):
    """Tracks one task per key. A second start for a live key is refused.

    The key doubles as the de-duplication identity: `generate:12:cv` cannot run
    twice at once, which is what stops a double-click costing two LLM calls.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._running = {}
        self._history = OrderedDict()

    def start(self, key, label, fn):
        """Run `fn()` on a daemon thread. Returns False if `key` is busy.

        `fn` may return a string, which becomes the task's summary.
        """
        with self._lock:
            if key in self._running:
                return False
            task = Task(key, label)
            self._running[key] = task

        def run():
            try:
                summary = fn()
                task.result = str(summary) if summary is not None else "done"
            except Exception as exc:
                log.exception("background task %s failed", key)
                task.error = "{0}: {1}".format(type(exc).__name__, exc)
            finally:
                task.finished_at = time.time()
                with self._lock:
                    self._running.pop(key, None)
                    self._history[key + ":" + str(task.started_at)] = task
                    while len(self._history) > HISTORY_LIMIT:
                        self._history.popitem(last=False)

        threading.Thread(target=run, daemon=True, name="jobhunt-" + key).start()
        return True

    def is_running(self, key):
        with self._lock:
            return key in self._running

    def running(self):
        with self._lock:
            return sorted(self._running.values(), key=lambda t: t.started_at)

    def recent(self, limit=5):
        with self._lock:
            tasks = list(self._history.values())
        return list(reversed(tasks))[:limit]

    def clear_history(self):
        with self._lock:
            self._history.clear()


#: Process-wide registry, shared by every route.
registry = Registry()
