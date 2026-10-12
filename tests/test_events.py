import queue
import unittest

from aihub.web.events import stream


class LiveAccessTest(unittest.TestCase):
    def test_revocation_stops_pending_private_events(self):
        pending = queue.Queue()
        allowed = [True]
        output = stream(pending, {}, lambda: allowed[0], ping_s=.01)
        self.assertIn('snapshot', next(output))
        allowed[0] = False
        pending.put({'type': 'text', 'chunk': 'private information'})
        revoked = next(output)
        self.assertIn('access_revoked', revoked)
        self.assertNotIn('private information', revoked)
        with self.assertRaises(StopIteration):
            next(output)

    def test_revocation_is_checked_even_without_new_events(self):
        allowed = [True]
        output = stream(queue.Queue(), {}, lambda: allowed[0], ping_s=.01)
        next(output)
        allowed[0] = False
        self.assertIn('access_revoked', next(output))
