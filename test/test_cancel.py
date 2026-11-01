"""Generation-scoped cancel tokens."""
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cancel import CancelScope


class CancelScopeTest(unittest.TestCase):
    def test_new_token_cancels_previous_but_not_itself(self):
        scope = CancelScope()
        first = scope.new()
        second = scope.new()
        self.assertTrue(first.cancelled)
        self.assertEqual(first.reason, 'superseded')
        self.assertFalse(second.cancelled)
        self.assertEqual((first.generation, second.generation), (1, 2))

    def test_trigger_cancels_current_before_its_own_capture_starts(self):
        scope = CancelScope()
        speaking = scope.new()
        scope.cancel_current('wake')       # T2 hit
        capture = scope.new()             # the capture the wake starts
        self.assertEqual(speaking.reason, 'wake')
        self.assertFalse(capture.cancelled)

    def test_first_reason_wins_and_wait_unblocks(self):
        scope = CancelScope()
        token = scope.new()
        waiter = threading.Thread(target=token.wait, args=(2,))
        waiter.start()
        scope.cancel_current('control')
        scope.cancel_current('mode')
        waiter.join(1)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(token.reason, 'control')

    def test_cancel_without_token_is_harmless(self):
        CancelScope().cancel_current('control')


if __name__ == '__main__':
    unittest.main()
