import unittest

from bridge_payloads import parse_bridge_payload


class BridgePayloadTests(unittest.TestCase):
    def test_accepts_and_normalizes_valid_payload(self) -> None:
        parsed = parse_bridge_payload(
            {
                "type": "poker_cards",
                "hole": ["as", "kd"],
                "board": ["7c", "2d", "th"],
                "players": 99,
                "reset": True,
                "handId": 42,
                "heroUserId": "123",
                "heroSeatId": 5,
                "heroSittingOut": False,
            }
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.hole_cards, ["AS", "KD"])
        self.assertEqual(parsed.board_cards, ["7C", "2D", "TH"])
        self.assertEqual(parsed.players_count, 10)
        self.assertTrue(parsed.reset_state)
        self.assertEqual(parsed.hand_id, 42)
        self.assertEqual(parsed.hero_user_id, 123)
        self.assertEqual(parsed.hero_seat_id, 5)
        self.assertFalse(parsed.hero_sitting_out)

    def test_rejects_payload_without_supported_fields(self) -> None:
        self.assertIsNone(parse_bridge_payload({"type": "poker_cards", "ignored": True}))

    def test_rejects_invalid_hole_shape(self) -> None:
        self.assertIsNone(parse_bridge_payload({"type": "poker_cards", "hole": ["as"]}))

    def test_clamps_player_count(self) -> None:
        low = parse_bridge_payload({"type": "poker_cards", "players": 1})
        high = parse_bridge_payload({"type": "poker_cards", "players": 20})

        self.assertIsNotNone(low)
        self.assertIsNotNone(high)
        assert low is not None
        assert high is not None
        self.assertEqual(low.players_count, 2)
        self.assertEqual(high.players_count, 10)


if __name__ == "__main__":
    unittest.main()
