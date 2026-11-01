"""Supervision helpers: heartbeats, restart budgets, drop-rate warnings, thread stats."""
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import worker_threads as wt


class HelperTest(unittest.TestCase):
    def test_heartbeat_goes_stale_unless_paused(self):
        beat = wt.Heartbeat('wake-word', 2)
        self.assertFalse(beat.stale(beat.last + 1.9))
        self.assertTrue(beat.stale(beat.last + 2.1))
        beat.paused = True
        self.assertFalse(beat.stale(beat.last + 10))

    def test_restart_budget_is_per_window(self):
        budget = wt.RestartBudget(3, 300)
        self.assertEqual([budget.allow(t) for t in (0, 10, 20, 30)], [True, True, True, False])
        self.assertTrue(budget.allow(301))

    def test_drop_rate_warns_only_for_sustained_loss(self):
        rate = wt.DropRate(.01, 10)
        self.assertIsNone(rate.check(0, 0, 0))
        self.assertIsNone(rate.check(250, 1, 5))       # window not over yet
        self.assertIsNone(rate.check(500, 2, 10))      # 0.4 %
        self.assertAlmostEqual(rate.check(1000, 22, 20), .04)  # 4 % over the next window

    def test_thread_cpu_stack_and_rss(self):
        stop = threading.Event()
        worker = threading.Thread(target=lambda: [None for _ in iter(lambda: stop.wait(.01), True)], name='busy-test')
        worker.start()
        try:
            self.assertIn('busy-test', wt.thread_cpu())
            self.assertIn('wait', wt.stack_of(worker))
        finally:
            stop.set(); worker.join()
        self.assertGreater(wt.rss_kb(), 1000)

    def test_join_all_reports_threads_that_do_not_stop(self):
        stop = threading.Event()
        stuck = threading.Thread(target=stop.wait, args=(5,), name='stuck-test', daemon=True)
        quick = threading.Thread(target=lambda: None, name='quick-test')
        stuck.start(); quick.start()
        started = time.monotonic()
        self.assertEqual(wt.join_all([stuck, quick, None], .1), ['stuck-test'])
        self.assertLess(time.monotonic() - started, 1)
        stop.set()


if __name__ == '__main__':
    unittest.main()
