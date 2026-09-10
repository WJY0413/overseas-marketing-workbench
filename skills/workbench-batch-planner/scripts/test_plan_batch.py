from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from plan_batch import resolve_timezone, route_history_eligibility


class RouteHistoryEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
        self.cutoff = self.now - timedelta(days=7)

    def test_never_sent_non_ready_route_is_eligible(self) -> None:
        eligible, source = route_history_eligibility(
            {"roll_status": "not_eligible", "roll_ready": False},
            None,
            self.cutoff,
        )
        self.assertTrue(eligible)
        self.assertEqual("derived_never_sent", source)

    def test_recent_route_send_blocks_even_if_source_says_ready(self) -> None:
        eligible, source = route_history_eligibility(
            {"roll_status": "ready", "roll_ready": True},
            self.now - timedelta(days=3),
            self.cutoff,
        )
        self.assertFalse(eligible)
        self.assertEqual("contact_7d_cooldown", source)

    def test_elapsed_route_cooldown_restores_non_ready_route(self) -> None:
        eligible, source = route_history_eligibility(
            {"roll_status": "not_eligible", "roll_ready": False},
            self.now - timedelta(days=8),
            self.cutoff,
        )
        self.assertTrue(eligible)
        self.assertEqual("derived_contact_cooldown_elapsed", source)

    def test_success_locked_route_remains_blocked(self) -> None:
        eligible, source = route_history_eligibility(
            {"roll_status": "success_locked", "roll_ready": False},
            None,
            self.cutoff,
        )
        self.assertFalse(eligible)
        self.assertEqual("source_success_locked", source)

    def test_london_timezone_fallback_tracks_summer_and_winter_offsets(self) -> None:
        summer = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
        winter = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(timedelta(hours=1), resolve_timezone("Europe/London", summer).utcoffset(summer))
        self.assertEqual(timedelta(0), resolve_timezone("Europe/London", winter).utcoffset(winter))


if __name__ == "__main__":
    unittest.main()
