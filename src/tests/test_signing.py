from __future__ import annotations

import unittest

from mavproto.signing import check_replay_window


class SigningReplayTests(unittest.TestCase):
    def test_rejects_duplicate_and_older_timestamp(self) -> None:
        seen: dict[tuple[int, int, int], int] = {}
        stream = (255, 190, 0)
        now = 10_000_000
        self.assertEqual(check_replay_window(seen, stream, now, now), "ok")
        self.assertEqual(check_replay_window(seen, stream, now, now), "replay")
        self.assertEqual(check_replay_window(seen, stream, now - 1, now), "replay")

    def test_rejects_timestamp_over_one_minute_old(self) -> None:
        seen: dict[tuple[int, int, int], int] = {}
        now = 10_000_000
        self.assertEqual(check_replay_window(seen, (1, 1, 0), now - 6_000_001, now),
                         "stale_timestamp")


if __name__ == "__main__":
    unittest.main()
