import unittest
from unittest.mock import Mock

from app import (
    BOARD_ORDER,
    Card,
    PreflopApp,
    _describe_current_hand_context,
    _recommend_facing_postflop_bet,
    _recommend_when_checked_to,
)
from handranker import analyze_postflop


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

    def test_player_count_keeps_updating_after_hero_folds(self) -> None:
        app = object.__new__(PreflopApp)
        app._hero_folded_waiting_for_new_hole = False
        app._awaiting_hero_hole_after_reset = False
        app._current_hand_id = 42
        app._hero_user_id = None
        app._hero_seat_id = 4
        app._hero_sitting_out = False
        app._strategy_players_count = 4
        app._street = "flop"
        app._pot_chips = None
        app._to_call_chips = None
        app._recent_actions = []
        app.selected = [Card("A", "s"), Card("K", "d")]
        app.board_cards = {slot: None for slot in BOARD_ORDER}
        app.players_var = Mock()
        app.server_info_var = Mock()
        app._log_app_action = Mock()
        app._append_server_log = Mock()
        app._next_open_board_slot = Mock(return_value="flop_1")
        app._refresh_buttons = Mock()
        app._refresh_hole_buttons = Mock()
        app._refresh_board_buttons = Mock()
        app._update_outputs = Mock()
        app._update_strategy_panel = Mock()
        app._write_strategy_training_event = Mock()

        PreflopApp._apply_external_cards(
            app,
            hole_cards=[],
            board_cards=None,
            players_count=2,
            hand_id=42,
            hero_seat_id=4,
            hero_folded=True,
        )

        app.players_var.set.assert_called_once_with(2)
        self.assertEqual(app._strategy_players_count, 2)
        self.assertTrue(app._hero_folded_waiting_for_new_hole)
        self.assertEqual(app.selected, [Card("A", "s"), Card("K", "d")])
        app._update_strategy_panel.assert_called_once()

    def test_external_snapshot_rebuilds_flop_and_decision_context(self) -> None:
        app = object.__new__(PreflopApp)
        app._hero_folded_waiting_for_new_hole = False
        app._awaiting_hero_hole_after_reset = False
        app._current_hand_id = 42
        app._hero_user_id = None
        app._hero_seat_id = 2
        app._hero_sitting_out = False
        app._hero_turn = False
        app._strategy_players_count = 3
        app._street = "preflop"
        app._pot_chips = None
        app._to_call_chips = None
        app._minimum_raise_chips = None
        app._recent_actions = []
        app.selected = []
        app.board_cards = {slot: None for slot in BOARD_ORDER}
        app.players_var = Mock()
        app.server_info_var = Mock()
        app._log_app_action = Mock()
        app._append_server_log = Mock()
        app._next_open_board_slot = Mock(return_value="turn")
        app._refresh_buttons = Mock()
        app._refresh_hole_buttons = Mock()
        app._refresh_board_buttons = Mock()
        app._update_outputs = Mock()
        app._update_strategy_panel = Mock()
        app._write_strategy_training_event = Mock()

        PreflopApp._apply_external_cards(
            app,
            hole_cards=["KD", "4D"],
            board_cards=["9S", "2D", "9H"],
            players_count=3,
            hand_id=42,
            hero_seat_id=2,
            pot_chips=25,
            to_call_chips=4,
            minimum_raise_chips=8,
            hero_turn=True,
        )

        self.assertEqual(app._street, "flop")
        self.assertEqual(app._pot_chips, 25)
        self.assertEqual(app._to_call_chips, 4)
        self.assertEqual(app._minimum_raise_chips, 8)
        self.assertTrue(app._hero_turn)
        self.assertEqual(app.selected, [Card("K", "d"), Card("4", "d")])

        PreflopApp._apply_external_cards(
            app,
            hole_cards=["KD", "4D"],
            board_cards=["9S", "2D", "9H"],
            players_count=3,
            hand_id=42,
            hero_seat_id=2,
            pot_chips=25,
            to_call_chips=4,
            minimum_raise_chips=8,
            hero_turn=True,
        )

        self.assertEqual(app.selected, [Card("K", "d"), Card("4", "d")])
        self.assertEqual(
            [app.board_cards[slot] for slot in BOARD_ORDER[:3]],
            [Card("9", "s"), Card("2", "d"), Card("9", "h")],
        )
        skip_events = [
            call
            for call in app._log_app_action.call_args_list
            if call.args and call.args[0] == "apply_external_cards_skip"
        ]
        self.assertEqual(skip_events, [])

    def test_unibet_raw_relax_frame_applies_cards_without_tagged_payload(self) -> None:
        app = object.__new__(PreflopApp)
        app._unibet_raw_hand_id = None
        app._unibet_raw_hole_key = None
        app._unibet_raw_hole_cards = None
        app._unibet_raw_board_key = None
        app._unibet_raw_board_cards = []
        app._unibet_raw_players_count = None
        app._unibet_raw_hero_sitting_out = None
        app._unibet_raw_hero_folded = None
        app._unibet_raw_hero_turn = None
        app._unibet_raw_pot = None
        app._unibet_raw_to_call = None
        app._unibet_raw_minimum_raise = None
        app._unibet_raw_reset_sent = False
        app._hero_seat_id = None
        app.hero_name_var = Mock()
        app.hero_name_var.get.return_value = "hero"
        app._log_app_action = Mock()
        app._apply_external_cards = Mock()

        line = (
            '[RAW_CONSOLE] [site:unibet_nl_pokerwebclient] '
            '[page:https://cf-mt-cdn1.relaxg.com/kenobi/clients/unibet/web/latest/ frame] '
            '[unsafeWindow:WS_MESSAGE] #existing-1 | wss://mclient.api.relaxg.com/wspoker/ | onmessage | '
            'text(400): <message><body>{&quot;tags&quot;:[&quot;deal&quot;],&quot;payLoad&quot;:{'
            '&quot;hid&quot;:42,'
            '&quot;c&quot;:[&quot;p0|p1|hero&quot;,[1,1,1],[100,100,100],[0,2,4],[],null,null,null],'
            '&quot;p&quot;:[&quot;table-instance&quot;,2,0,&quot;kd4d&quot;,null,null]'
            '}}</body></message>'
        )

        self.assertTrue(PreflopApp._process_unibet_raw_relax_line(app, line))
        app._apply_external_cards.assert_called_once_with(
            ["KD", "4D"],
            [],
            3,
            True,
            hand_id=42,
            table_id=None,
            hero_user_id=None,
            hero_seat_id=2,
            hero_sitting_out=False,
            hero_folded=False,
            pot_chips=6,
            to_call_chips=0,
            minimum_raise_chips=None,
            hero_turn=False,
        )

        app._apply_external_cards.reset_mock()
        flop_line = (
            '[RAW_CONSOLE] [site:unibet_nl_pokerwebclient] '
            '[page:https://cf-mt-cdn1.relaxg.com/kenobi/clients/unibet/web/latest/ frame] '
            '[unsafeWindow:WS_MESSAGE] #existing-1 | wss://mclient.api.relaxg.com/wspoker/ | onmessage | '
            'text(400): <message><body>{&quot;tags&quot;:[&quot;flop&quot;],&quot;payLoad&quot;:{'
            '&quot;hid&quot;:42,'
            '&quot;c&quot;:[&quot;p0|p1|hero&quot;,[3,1,1],[100,100,100],[0,0,0],[[25,1]],null,null,&quot;9s2d9h&quot;],'
            '&quot;p&quot;:[&quot;table-instance&quot;,2,0,null,null,null]'
            '}}</body></message>'
        )

        self.assertTrue(PreflopApp._process_unibet_raw_relax_line(app, flop_line))
        app._apply_external_cards.assert_called_once_with(
            [],
            ["9S", "2D", "9H"],
            2,
            False,
            hand_id=42,
            table_id=None,
            hero_user_id=None,
            hero_seat_id=2,
            hero_sitting_out=False,
            hero_folded=False,
            pot_chips=25,
            to_call_chips=None,
            minimum_raise_chips=None,
            hero_turn=False,
        )

    def test_unibet_raw_relax_ignores_hole_from_non_hero_named_seat(self) -> None:
        app = object.__new__(PreflopApp)
        app._unibet_raw_hand_id = None
        app._unibet_raw_hole_key = None
        app._unibet_raw_hole_cards = None
        app._unibet_raw_board_key = None
        app._unibet_raw_board_cards = []
        app._unibet_raw_players_count = None
        app._unibet_raw_hero_sitting_out = None
        app._unibet_raw_hero_folded = None
        app._unibet_raw_hero_turn = None
        app._unibet_raw_pot = None
        app._unibet_raw_to_call = None
        app._unibet_raw_minimum_raise = None
        app._unibet_raw_reset_sent = False
        app._hero_seat_id = None
        app.hero_name_var = Mock()
        app.hero_name_var.get.return_value = "hero"
        app._log_app_action = Mock()
        app._apply_external_cards = Mock()

        line = (
            '[RAW_CONSOLE] [site:unibet_nl_pokerwebclient] '
            '[page:https://cf-mt-cdn1.relaxg.com/kenobi/clients/unibet/web/latest/ frame] '
            '[unsafeWindow:WS_MESSAGE] #existing-1 | wss://mclient.api.relaxg.com/wspoker/ | onmessage | '
            'text(400): <message><body>{&quot;tags&quot;:[&quot;deal&quot;],&quot;payLoad&quot;:{'
            '&quot;hid&quot;:43,'
            '&quot;c&quot;:[&quot;hero|villain|p2&quot;,[1,1,1],[100,100,100],[0,2,4],[],null,null,null],'
            '&quot;p&quot;:[&quot;table-instance&quot;,2,0,&quot;kd4d&quot;,null,null]'
            '}}</body></message>'
        )

        self.assertTrue(PreflopApp._process_unibet_raw_relax_line(app, line))
        args, kwargs = app._apply_external_cards.call_args
        self.assertEqual(args[0], [])
        self.assertEqual(kwargs["hero_seat_id"], 0)
        self.assertFalse(app._unibet_raw_hole_cards)

    def test_external_cards_lock_to_first_verified_table(self) -> None:
        app = object.__new__(PreflopApp)
        app._hero_folded_waiting_for_new_hole = False
        app._awaiting_hero_hole_after_reset = True
        app._current_hand_id = None
        app._current_table_id = None
        app._bridge_card_table_id = None
        app._hero_user_id = None
        app._hero_seat_id = None
        app._hero_sitting_out = None
        app._hero_turn = None
        app._strategy_players_count = None
        app._street = "preflop"
        app._pot_chips = None
        app._to_call_chips = None
        app._minimum_raise_chips = None
        app._recent_actions = []
        app.selected = []
        app.board_cards = {slot: None for slot in BOARD_ORDER}
        app.players_var = Mock()
        app.server_info_var = Mock()
        app._set_tracker_table_id = Mock()
        app._log_app_action = Mock()
        app._append_server_log = Mock()
        app._next_open_board_slot = Mock(return_value="flop_1")
        app._refresh_buttons = Mock()
        app._refresh_hole_buttons = Mock()
        app._refresh_board_buttons = Mock()
        app._update_outputs = Mock()
        app._update_strategy_panel = Mock()
        app._write_strategy_training_event = Mock()

        PreflopApp._apply_external_cards(
            app,
            hole_cards=["KD", "4D"],
            board_cards=[],
            players_count=3,
            reset_state=True,
            hand_id=42,
            table_id="100",
        )

        self.assertEqual(app._bridge_card_table_id, "100")
        self.assertEqual(app.selected, [Card("K", "d"), Card("4", "d")])

        PreflopApp._apply_external_cards(
            app,
            hole_cards=["AS", "AH"],
            board_cards=[],
            players_count=3,
            hand_id=43,
            table_id="200",
        )

        self.assertEqual(app.selected, [Card("K", "d"), Card("4", "d")])
        skip_reasons = [
            call.kwargs.get("reason")
            for call in app._log_app_action.call_args_list
            if call.args and call.args[0] == "apply_external_cards_skip"
        ]
        self.assertIn("bridge_table_mismatch", skip_reasons)

    def test_completed_equity_refreshes_strategy_panel(self) -> None:
        app = object.__new__(PreflopApp)
        cache_key = ("Kd", "4d", ("9s", "2d", "9h"), 3)
        app.odds_after_id = "scheduled"
        app.odds_cache = {cache_key: (0.42, 0.03, 0.55)}
        app.selected = [Card("K", "d"), Card("4", "d")]
        app.odds_status_var = Mock()
        app.win_var = Mock()
        app.tie_var = Mock()
        app.loss_var = Mock()
        app.equity_var = Mock()
        app.equity_label = None
        app._odds_cache_key = Mock(return_value=cache_key)
        app._update_strategy_panel = Mock()

        PreflopApp._run_odds_update(app, players=3, board_codes=["9s", "2d", "9h"])

        app.equity_var.set.assert_called_once_with("Total equity: 45.0%")
        app._update_strategy_panel.assert_called_once_with()

    def test_random_equity_does_not_turn_ace_high_into_a_call(self) -> None:
        profile = analyze_postflop(["Kd", "6d"], ["8h", "Ad", "5s"])

        headline, _reason, target = _recommend_facing_postflop_bet(
            profile,
            equity=0.49,
            pot_odds=0.265,
            players=2,
        )

        self.assertEqual(headline, "WEAK HAND VS BET. FOLD.")
        self.assertGreater(target, 0.265)

    def test_open_ended_draw_folds_when_direct_price_is_too_high(self) -> None:
        profile = analyze_postflop(["7c", "6c"], ["Td", "4s", "5s"])

        headline, _reason, _target = _recommend_facing_postflop_bet(
            profile,
            equity=0.43,
            pot_odds=0.29,
            players=2,
        )

        self.assertEqual(headline, "DRAW, BUT PRICE IS TOO HIGH. FOLD.")

    def test_open_ended_draw_can_call_a_small_price(self) -> None:
        profile = analyze_postflop(["7c", "6c"], ["Td", "4s", "5s"])

        headline, _reason, _target = _recommend_facing_postflop_bet(
            profile,
            equity=0.43,
            pot_odds=0.18,
            players=2,
        )

        self.assertEqual(headline, "STRONG DRAW. CALL AT THIS PRICE.")

    def test_set_can_raise_for_value(self) -> None:
        profile = analyze_postflop(["3h", "3s"], ["7s", "3c", "2c"])

        headline, _reason, _target = _recommend_facing_postflop_bet(
            profile,
            equity=0.94,
            pot_odds=0.227,
            players=2,
        )

        self.assertEqual(headline, "STRONG MADE HAND. RAISE FOR VALUE.")

    def test_flush_draw_is_not_described_as_made_value(self) -> None:
        profile = analyze_postflop(["Qs", "6s"], ["8s", "Jd", "9s"])

        headline, _reason = _recommend_when_checked_to(profile)

        self.assertEqual(headline, "STRONG DRAW. CHECK OR BET SMALL.")

    def test_board_pair_is_not_mistaken_for_air_label(self) -> None:
        app = object.__new__(PreflopApp)
        details = ["Your hole cards have not made a pair."]

        headline, returned_details = PreflopApp._enforce_advice_consistency(
            app,
            "BOARD PAIR ONLY. CHECK.",
            details,
            "Pair of Queens",
            to_call=0,
        )

        self.assertEqual(headline, "BOARD PAIR ONLY. CHECK.")
        self.assertEqual(returned_details, details)

    def test_current_hand_marks_pair_that_comes_from_board(self) -> None:
        self.assertEqual(
            _describe_current_hand_context(["3h", "As"], ["Qd", "Tc", "Qh"]),
            "Pair of Queens (pair is on the board)",
        )


if __name__ == "__main__":
    unittest.main()
