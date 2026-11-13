import unittest
from tests.test_control_plane import cp


class RecoveryTransitionTests(unittest.TestCase):
    def test_cancel_is_idempotent_only_for_cancelled_terminal_operation(self):
        states = cp.update_recovery
        op = states.start(None, 100, 'a')
        self.assertFalse(states.cancel(None, 'a', 101)[1])
        for terminal in states.TERMINAL:
            self.assertEqual(states.cancel({**op, 'state': terminal}, 'a', 101)[1], terminal == 'cancelled')
        for invalid in ('cancelled', 'expired', 'requested', 'cancelling'):
            self.assertFalse(states.progress(op, 'a', invalid, 101)[1])
        self.assertFalse(states.progress(None, 'a', 'checking', 101)[1])
        self.assertFalse(states.progress(op, 'a', 'nonsense', 101)[1])
