import unittest
from queue import Queue
from unittest.mock import Mock

from app import (
    BOARD_ORDER,
    Card,
    PreflopApp,
    _bridge_site_from_line,
    _bridge_site_from_payload,
    _is_low_value_strategy_bridge_line,
    _describe_current_hand_context,
    _recommend_facing_postflop_bet,
    _recommend_when_checked_to,
)
from handranker import analyze_postflop


class TrackerTests(unittest.TestCase):
    def test_low_value_strategy_bridge_lines_are_filtered(self) -> None:
        prefix = "[RAW_CONSOLE] [unsafeWindow:POKER_EVENT] event | "
        self.assertTrue(_is_low_value_strategy_bridge_line(prefix + '{"updates":[{"action":"fold"}]}'))
        self.assertTrue(_is_low_value_strategy_bridge_line(prefix + '{"action":"ping"}'))
        self.assertTrue(_is_low_value_strategy_bridge_line(prefix + '{"action":"chatMessage"}'))
        self.assertFalse(_is_low_value_strategy_bridge_line(prefix + '{"action":"fold"}'))
        self.assertFalse(_is_low_value_strategy_bridge_line('TM_BRIDGE:{"reset":true}'))

    def test_server_queue_is_drained_in_bounded_batches(self) -> None:
        app = object.__new__(PreflopApp)
        app._incoming_logs = Queue()
        for index in range(40):
            app._incoming_logs.put(f"bridge line {index}")
        app._append_server_log = Mock()
        app._log_app_action = Mock()
        app._process_console_line = Mock()

        processed = PreflopApp._drain_server_queue_batch(app)

        self.assertEqual(processed, 32)
        self.assertEqual(app._incoming_logs.qsize(), 8)
        self.assertEqual(app._process_console_line.call_count, 32)

    def test_bridge_site_is_extracted_from_tagged_and_raw_lines(self) -> None:
        self.assertEqual(
            _bridge_site_from_payload({"site": "unibet_nl_pokerwebclient"}),
            "unibet_nl_pokerwebclient",
        )
        self.assertEqual(
            _bridge_site_from_line(
                'TM_BRIDGE:{"type":"poker_cards","site":"casino_org_replaypoker"}'
            ),
            "casino_org_replaypoker",
        )
        self.assertEqual(
            _bridge_site_from_line(
                "[RAW_CONSOLE] [site:unibet_nl_pokerwebclient] [page:https://example.test top] event"
            ),
            "unibet_nl_pokerwebclient",
        )
        self.assertIsNone(_bridge_site_from_line('TM_BRIDGE:{"type":"poker_cards"}'))

    def test_process_console_line_rejects_unselected_site_before_applying_cards(self) -> None:
        app = object.__new__(PreflopApp)
        app.site_var = Mock()
        app.site_var.get.return_value = "unibet.nl/pokerwebclient"
        app._log_app_action = Mock()
        app._append_server_log = Mock()
        app._apply_external_cards = Mock()

        PreflopApp._process_console_line(
            app,
            'TM_BRIDGE:{"type":"poker_cards","site":"casino_org_replaypoker",'
            '"bridgeVersion":"2.8","hole":["As","Ah"],"handId":99}',
        )

        app._apply_external_cards.assert_not_called()
        self.assertEqual(
            app._log_app_action.call_args_list[-1].kwargs["reason"],
            "bridge_site_mismatch",
        )

    def test_process_console_line_accepts_selected_site(self) -> None:
        app = object.__new__(PreflopApp)
        app.site_var = Mock()
        app.site_var.get.return_value = "unibet.nl/pokerwebclient"
        app._log_app_action = Mock()
        app._append_server_log = Mock()
        app._apply_external_cards = Mock()

        PreflopApp._process_console_line(
            app,
            'TM_BRIDGE:{"type":"poker_cards","site":"unibet_nl_pokerwebclient",'
            '"bridgeVersion":"2.8","hole":["As","Ah"],"handId":99}',
        )

        app._apply_external_cards.assert_called_once()
        args, kwargs = app._apply_external_cards.call_args
        self.assertEqual(args[:2], (["AS", "AH"], None))
        self.assertEqual(kwargs["hand_id"], 99)

    def test_process_console_line_rejects_unlabelled_legacy_raw_state(self) -> None:
        app = object.__new__(PreflopApp)
        app.site_var = Mock()
        app.site_var.get.return_value = "unibet.nl/pokerwebclient"
        app._log_app_action = Mock()
        app._update_strategy_from_console_line = Mock()
        app._process_unibet_raw_relax_line = Mock()

        PreflopApp._process_console_line(
            app,
            '[RAW_CONSOLE] [unsafeWindow:POKER_EVENT] event | {"action":"startHand"}',
        )

        app._update_strategy_from_console_line.assert_not_called()
        app._process_unibet_raw_relax_line.assert_not_called()
        self.assertEqual(
            app._log_app_action.call_args_list[-1].kwargs["reason"],
            "missing_bridge_site",
        )

    def test_site_change_reset_clears_previous_source_identity(self) -> None:
        app = object.__new__(PreflopApp)
        app._session_active = False
        app._current_table_id = "casino-table"
        app._bridge_card_table_id = "casino-table"
        app._current_hand_id = 1401245656
        app._hero_user_id = 4413012
        app._hero_seat_id = 3
        app._hero_sitting_out = False
        app._hero_turn = True
        app._pot_chips = 850
        app._to_call_chips = 390
        app._minimum_raise_chips = 390
        app._big_blind_chips = 20
        app._strategy_players_count = 5
        app._allin_pressure = True
        app._hero_acted_preflop = True
        app._recent_actions = ["raise"]
        app._seat_user_map = {3: 4413012}
        app._player_screen_names = {4413012: "hero"}
        app._reset_hand_learning_state = Mock()
        app._clear_all_cards = Mock()
        app._update_strategy_panel = Mock()
        app._refresh_player_tracker_panel = Mock()

        PreflopApp._reset_bridge_source_state(app)

        self.assertIsNone(app._current_table_id)
        self.assertIsNone(app._bridge_card_table_id)
        self.assertIsNone(app._current_hand_id)
        self.assertIsNone(app._hero_user_id)
        self.assertIsNone(app._big_blind_chips)
        self.assertTrue(app._awaiting_hero_hole_after_reset)
        app._clear_all_cards.assert_called_once_with()

    def _strategy_app(
        self,
        hole: list[Card],
        *,
        to_call: int | None = 0,
        pot: int | None = 6,
        players: int = 3,
    ) -> PreflopApp:
        app = object.__new__(PreflopApp)
        app._hero_folded_waiting_for_new_hole = False
        app._awaiting_hero_hole_after_reset = False
        app._hero_sitting_out = False
        app._hero_turn = True
        app._current_hand_id = 9001
        app._street = "preflop"
        app._strategy_players_count = players
        app._pot_chips = pot
        app._to_call_chips = to_call
        app._minimum_raise_chips = None
        app._big_blind_chips = 4
        app._allin_pressure = False
        app._hero_acted_preflop = False
        app._recent_actions = []
        app._decision_advice_locks = {}
        app.selected = hole
        app.board_cards = {slot: None for slot in BOARD_ORDER}
        app.players_var = Mock()
        app.players_var.get.return_value = players
        app.strategy_context_var = Mock()
        app.training_status_var = Mock()
        app.strategy_quick_var = Mock()
        app.strategy_quick_sub_var = Mock()
        app.strategy_advice_var = Mock()
        app.equity_var = Mock()
        app.equity_var.get.return_value = "Total equity: -"
        app.strategy_quick_label = None
        app.strategy_quick_sub_label = None
        app._capture_advice_snapshot = Mock()
        app._log_app_action = Mock()
        return app

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

        PreflopApp._apply_external_cards(
            app,
            hole_cards=[],
            board_cards=[],
            players_count=3,
            reset_state=True,
            hand_id=43,
        )

        self.assertFalse(app._hero_folded_waiting_for_new_hole)
        self.assertTrue(app._awaiting_hero_hole_after_reset)
        self.assertEqual(app.selected, [])

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

    def test_unibet_raw_relax_new_named_hero_hand_sends_hole_from_act_frame(self) -> None:
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
            'text(400): <message><body>{&quot;tags&quot;:[&quot;act&quot;],&quot;payLoad&quot;:{'
            '&quot;hid&quot;:44,&quot;tid&quot;:900,'
            '&quot;c&quot;:[&quot;p0|p1|hero&quot;,[1,1,1],[100,100,100],[0,2,4],[],null,null,null],'
            '&quot;d&quot;:[2,2,4],'
            '&quot;p&quot;:[&quot;table-instance&quot;,2,0,&quot;asah&quot;,null,null]'
            '}}</body></message>'
        )

        self.assertTrue(PreflopApp._process_unibet_raw_relax_line(app, line))
        app._apply_external_cards.assert_called_once_with(
            ["AS", "AH"],
            [],
            3,
            True,
            hand_id=44,
            table_id="900",
            hero_user_id=None,
            hero_seat_id=2,
            hero_sitting_out=None,
            hero_folded=False,
            pot_chips=6,
            to_call_chips=0,
            minimum_raise_chips=None,
            hero_turn=False,
        )

    def test_external_cards_relocks_to_newer_table_hand(self) -> None:
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

        self.assertEqual(app._bridge_card_table_id, "200")
        self.assertEqual(app.selected, [Card("A", "s"), Card("A", "h")])
        relock_events = [
            call
            for call in app._log_app_action.call_args_list
            if call.args and call.args[0] == "bridge_table_relocked"
        ]
        self.assertEqual(len(relock_events), 1)
        self.assertEqual(relock_events[0].kwargs.get("previous_table_id"), "100")
        self.assertEqual(relock_events[0].kwargs.get("table_id"), "200")

        PreflopApp._apply_external_cards(
            app,
            hole_cards=["QC", "QD"],
            board_cards=[],
            players_count=3,
            hand_id=42,
            table_id="100",
        )

        self.assertEqual(app._bridge_card_table_id, "200")
        self.assertEqual(app.selected, [Card("A", "s"), Card("A", "h")])
        skip_reasons = [
            call.kwargs.get("reason")
            for call in app._log_app_action.call_args_list
            if call.args and call.args[0] == "apply_external_cards_skip"
        ]
        self.assertIn("bridge_table_mismatch", skip_reasons)

        PreflopApp._apply_external_cards(
            app,
            hole_cards=[],
            board_cards=["JC", "TS", "TH"],
            players_count=3,
            hand_id=44,
            table_id="300",
        )

        self.assertEqual(app._bridge_card_table_id, "300")
        self.assertEqual(app.selected, [])
        self.assertEqual(
            [app.board_cards[slot] for slot in ("flop_1", "flop_2", "flop_3")],
            [Card("J", "c"), Card("T", "s"), Card("T", "h")],
        )

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

        self.assertEqual(headline, "STRONG DRAW. BET 33% POT OR CHECK.")

    def test_paired_board_two_pair_calls_without_reraising(self) -> None:
        profile = analyze_postflop(["Ah", "5h"], ["Jd", "Jc", "2h", "Ac"])

        headline, _reason, _target = _recommend_facing_postflop_bet(
            profile,
            equity=0.83,
            pot_odds=0.255,
            players=2,
        )

        self.assertEqual(headline, "PAIRED-BOARD TWO PAIR. CALL ONLY. POT CONTROL.")

    def test_lower_pair_checks_when_action_is_free(self) -> None:
        profile = analyze_postflop(["9c", "7h"], ["9h", "Qs", "2d"])

        headline, _reason = _recommend_when_checked_to(profile, "flop")

        self.assertEqual(headline, "MARGINAL PAIR. CHECK.")

    def test_two_pair_uses_blocking_size_on_four_liner(self) -> None:
        profile = analyze_postflop(["Qh", "9s"], ["2c", "Qs", "Ks", "Jc", "9c"])

        headline, _reason = _recommend_when_checked_to(profile, "river")

        self.assertEqual(headline, "TWO PAIR ON FOUR-LINER. BET 33% POT OR CHECK.")

    def test_strategy_panel_shows_exact_paired_board_bet_size(self) -> None:
        app = self._strategy_app([Card("A", "h"), Card("5", "h")], to_call=0, pot=7, players=2)
        app._street = "turn"
        app.board_cards.update(
            {
                "flop_1": Card("J", "d"),
                "flop_2": Card("J", "c"),
                "flop_3": Card("2", "h"),
                "turn": Card("A", "c"),
            }
        )
        app.equity_var.get.return_value = "Total equity: 83.0%"

        PreflopApp._update_strategy_panel(app)

        app.strategy_quick_var.set.assert_called_with("BET")
        app.strategy_quick_sub_var.set.assert_called_with("SIZE ~2 | 33% POT")
        advice = app.strategy_advice_var.set.call_args.args[0]
        self.assertTrue(advice.startswith("PAIRED-BOARD TWO PAIR. BET 33% POT."))
        self.assertIn("Target about 2 chips", advice)

    def test_unibet_outgoing_action_is_written_to_training_log(self) -> None:
        app = object.__new__(PreflopApp)
        app._current_hand_id = 42
        app._street = "flop"
        app._hero_acted_preflop = False
        app._hero_folded_waiting_for_new_hole = False
        app._unibet_raw_seen_hero_actions = set()
        app.selected = [Card("A", "h"), Card("5", "h")]
        app._write_strategy_training_event = Mock()
        app._log_app_action = Mock()

        handled = PreflopApp._record_unibet_raw_strategy_body(
            app,
            {"action": "call", "params": {"hid": 42, "aid": 7, "cost": 12}},
        )

        self.assertTrue(handled)
        app._write_strategy_training_event.assert_called_once_with(
            "hero_action",
            action="call",
            chips=12,
            action_id=7,
            source="unibet_relax",
        )

    def test_unibet_finished_frame_writes_stack_delta(self) -> None:
        app = object.__new__(PreflopApp)
        app._hero_seat_id = 2
        app._unibet_raw_hero_start_stack = 100
        app._unibet_raw_hand_end_logged = False
        app._hand_hero_winnings = None
        app._hand_outcome = "unknown"
        app._hero_hand_start_stack = None
        app._hero_hand_end_stack = None
        app.hero_name_var = Mock()
        app.hero_name_var.get.return_value = "hero"
        app._write_strategy_training_event = Mock()
        app._log_app_action = Mock()

        handled = PreflopApp._record_unibet_raw_strategy_body(
            app,
            {
                "tags": ["finished", "winner"],
                "payLoad": {
                    "hid": 42,
                    "c": ["p0|p1|hero", [3, 3, 1], [80, 90, 140]],
                },
            },
        )

        self.assertTrue(handled)
        self.assertEqual(app._hand_hero_winnings, 40)
        self.assertEqual(app._hand_outcome, "won")
        app._write_strategy_training_event.assert_called_once_with(
            "hand_end",
            outcome="won",
            hero_winnings=40,
            hero_start_stack=100,
            hero_end_stack=140,
            source="unibet_relax",
        )

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

    def test_preflop_free_weak_hand_checks_instead_of_folds(self) -> None:
        app = self._strategy_app([Card("3", "d"), Card("5", "h")], to_call=0, pot=10)

        PreflopApp._update_strategy_panel(app)

        app.strategy_quick_var.set.assert_called_with("CHECK")
        advice = app.strategy_advice_var.set.call_args.args[0]
        self.assertTrue(advice.startswith("TRASH HAND. CHECK FREE OPTION."))

    def test_check_if_free_headline_maps_to_check_action(self) -> None:
        app = object.__new__(PreflopApp)

        recommendation = PreflopApp._recommendation_from_headline(
            app,
            "SPECULATIVE HAND. CHECK IF FREE, OTHERWISE FOLD.",
        )

        self.assertEqual(recommendation, "check")

    def test_playable_preflop_hand_continues_against_one_blind(self) -> None:
        app = self._strategy_app([Card("8", "d"), Card("8", "c")], to_call=2, pot=10, players=3)

        PreflopApp._update_strategy_panel(app)

        app.strategy_quick_var.set.assert_called_with("CALL / RAISE")
        advice = app.strategy_advice_var.set.call_args.args[0]
        self.assertTrue(advice.startswith("PLAYABLE HAND. CALL OR RAISE SMALL."))

    def test_one_big_blind_is_not_called_real_pressure(self) -> None:
        app = self._strategy_app([Card("K", "h"), Card("9", "h")], to_call=4, pot=6, players=3)

        PreflopApp._update_strategy_panel(app)

        advice = app.strategy_advice_var.set.call_args.args[0]
        self.assertTrue(advice.startswith("PLAYABLE HAND. CALL OR RAISE SMALL."))

    def test_strong_hand_raises_unopened_big_blind_price(self) -> None:
        app = self._strategy_app([Card("A", "s"), Card("K", "d")], to_call=4, pot=6, players=4)
        app._minimum_raise_chips = 8

        PreflopApp._update_strategy_panel(app)

        app.strategy_quick_var.set.assert_called_with("RAISE")
        app.strategy_quick_sub_var.set.assert_called_with("TARGET ~12 | 3 BB")
        advice = app.strategy_advice_var.set.call_args.args[0]
        self.assertTrue(advice.startswith("STRONG HAND. RAISE TO ABOUT 12."))


if __name__ == "__main__":
    unittest.main()
