import unittest

from app import Card, PreflopApp


class TrackerTests(unittest.TestCase):
    def _app_with_active_hand(self) -> PreflopApp:
        app = object.__new__(PreflopApp)
        app._active_hand_record = {
            "hand_id": 1001,
            "players": {},
            "final_pot": None,
        }
        app._current_hand_id = 1001
        app._pot_chips = None
        app._current_hand_player_deltas = {}
        app._current_hand_player_contrib = {}
        app._current_hand_player_won = {}
        app._current_hand_player_stacks = {}
        app._hero_user_id = 77
        app._hero_seat_id = 4
        app._hero_hand_start_stack = 1000
        app._hero_hand_end_stack = 1125
        app._hand_hero_winnings = 125
        app.selected = [Card("A", "s"), Card("K", "d")]
        app._session_hands_buffer = []
        app._session_table_delta = 0
        return app

    def test_flush_uses_computed_hero_winnings_when_award_maps_are_missing(self) -> None:
        app = self._app_with_active_hand()

        PreflopApp._flush_hand_tracker(app)

        self.assertEqual(app._session_table_delta, 125)
        self.assertEqual(len(app._session_hands_buffer), 1)
        record = app._session_hands_buffer[0]
        self.assertEqual(record["hero_delta"], 125)
        hero = record["players"][77]
        self.assertEqual(hero["starting_stack"], 1000)
        self.assertEqual(hero["ending_stack"], 1125)
        self.assertEqual(hero["hole_cards"], "As Kd")
        self.assertEqual(hero["amount_won"], 125)
        self.assertEqual(hero["amount_contributed"], 0)
        self.assertTrue(hero["is_winner"])

    def test_flush_preserves_explicit_contribution_maps_over_fallback(self) -> None:
        app = self._app_with_active_hand()
        app._current_hand_player_deltas = {77: -40}
        app._current_hand_player_contrib = {77: 40}
        app._current_hand_player_won = {77: 0}
        app._hand_hero_winnings = 125

        PreflopApp._flush_hand_tracker(app)

        record = app._session_hands_buffer[0]
        hero = record["players"][77]
        self.assertEqual(record["hero_delta"], -40)
        self.assertEqual(hero["amount_won"], 0)
        self.assertEqual(hero["amount_contributed"], 40)
        self.assertFalse(hero["is_winner"])


if __name__ == "__main__":
    unittest.main()
