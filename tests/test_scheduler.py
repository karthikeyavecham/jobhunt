"""Notification window, cycle isolation, and the overlap guard."""

import unittest
from datetime import datetime

from zoneinfo import ZoneInfo

from jobhunt import scheduler

BERLIN = ZoneInfo("Europe/Berlin")


def at(hour, minute=0):
    return datetime(2026, 8, 6, hour, minute, tzinfo=BERLIN)


class TestParseHhmm(unittest.TestCase):
    def test_parses_a_normal_time(self):
        self.assertEqual(scheduler.parse_hhmm("08:30", "00:00").hour, 8)

    def test_bad_input_falls_back_rather_than_raising(self):
        """A typo in config must not stop the scheduler from starting."""
        self.assertEqual(scheduler.parse_hhmm("not-a-time", "07:15").hour, 7)
        self.assertEqual(scheduler.parse_hhmm(None, "07:15").minute, 15)


class TestNotifyWindow(unittest.TestCase):
    SETTINGS = {
        "schedule.timezone": "Europe/Berlin",
        "slack.notify_from": "08:00",
        "slack.notify_until": "22:00",
    }

    def test_inside_the_window(self):
        for hour in (8, 12, 18, 22):
            self.assertTrue(
                scheduler.in_notify_window(self.SETTINGS, at(hour)), hour)

    def test_outside_the_window(self):
        for hour in (0, 3, 6, 7, 23):
            self.assertFalse(
                scheduler.in_notify_window(self.SETTINGS, at(hour)), hour)

    def test_window_that_wraps_midnight(self):
        settings = dict(self.SETTINGS,
                        **{"slack.notify_from": "22:00", "slack.notify_until": "06:00"})
        self.assertTrue(scheduler.in_notify_window(settings, at(23)))
        self.assertTrue(scheduler.in_notify_window(settings, at(2)))
        self.assertFalse(scheduler.in_notify_window(settings, at(12)))

    def test_zero_width_window_means_always(self):
        settings = dict(self.SETTINGS,
                        **{"slack.notify_from": "00:00", "slack.notify_until": "00:00"})
        self.assertTrue(scheduler.in_notify_window(settings, at(3)))

    def test_unknown_timezone_falls_back(self):
        settings = dict(self.SETTINGS, **{"schedule.timezone": "Mars/Olympus"})
        self.assertIsInstance(scheduler.in_notify_window(settings, at(12)), bool)

    def test_naive_datetime_is_localised(self):
        naive = datetime(2026, 8, 6, 12, 0)
        self.assertTrue(scheduler.in_notify_window(self.SETTINGS, naive))


class TestCycleResult(unittest.TestCase):
    def test_summary_mentions_held_notifications(self):
        result = scheduler.CycleResult()
        result.notified_skipped = True
        self.assertIn("held", result.summary())

    def test_summary_reports_cards_when_sent(self):
        result = scheduler.CycleResult()
        result.cards_posted = 3
        self.assertIn("3 cards", result.summary())

    def test_summary_surfaces_failures(self):
        result = scheduler.CycleResult()
        result.score_failed = 2
        result.errors.append({"stage": "fetch", "error": "boom"})
        summary = result.summary()
        self.assertIn("2 scoring failures", summary)
        self.assertIn("1 source errors", summary)


class TestOverlapGuard(unittest.TestCase):
    def test_a_second_cycle_is_skipped_while_one_runs(self):
        """A slow cycle must not be re-entered by the next tick."""
        scheduler._cycle_lock.acquire()
        try:
            result = scheduler.run_cycle(trigger="schedule")
        finally:
            scheduler._cycle_lock.release()

        self.assertEqual(result.fetched, 0)
        self.assertEqual(result.errors[0]["stage"], "lock")

    def test_the_lock_is_released_after_a_cycle(self):
        self.assertTrue(scheduler._cycle_lock.acquire(blocking=False))
        scheduler._cycle_lock.release()


if __name__ == "__main__":
    unittest.main()
