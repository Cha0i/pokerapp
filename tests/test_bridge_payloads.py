import unittest

from bridge_payloads import parse_bridge_payload, redact_bridge_log_line


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
                "tableId": 9001,
                "heroUserId": "123",
                "heroSeatId": 5,
                "heroSittingOut": False,
                "heroFolded": False,
                "pot": 24,
                "toCall": 6,
                "minimumRaise": 12,
                "heroTurn": True,
            }
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.hole_cards, ["AS", "KD"])
        self.assertEqual(parsed.board_cards, ["7C", "2D", "TH"])
        self.assertEqual(parsed.players_count, 10)
        self.assertTrue(parsed.reset_state)
        self.assertEqual(parsed.hand_id, 42)
        self.assertEqual(parsed.table_id, "9001")
        self.assertEqual(parsed.hero_user_id, 123)
        self.assertEqual(parsed.hero_seat_id, 5)
        self.assertFalse(parsed.hero_sitting_out)
        self.assertFalse(parsed.hero_folded)
        self.assertEqual(parsed.pot_chips, 24)
        self.assertEqual(parsed.to_call_chips, 6)
        self.assertEqual(parsed.minimum_raise_chips, 12)
        self.assertTrue(parsed.hero_turn)

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

    def test_redacts_bridge_credentials(self) -> None:
        line = (
            'https://client.example/?ticket=secret&lang=nl | '
            '<auth mechanism="PLAIN">encoded-secret</auth> | '
            '{"token":"json-secret","relaxtoken":"relax-secret","action":"deal"} | '
            '{&quot;relaxtoken&quot;:&quot;entity-secret&quot;} | '
            'authorization: Bearer bearer-secret'
        )

        redacted = redact_bridge_log_line(line)

        self.assertNotIn("secret", redacted)
        self.assertIn("ticket=[redacted]&lang=nl", redacted)
        self.assertIn('<auth mechanism="PLAIN">[redacted]</auth>', redacted)
        self.assertIn('"token":"[redacted]"', redacted)
        self.assertIn('"relaxtoken":"[redacted]"', redacted)
        self.assertIn('&quot;relaxtoken&quot;:&quot;[redacted]&quot;', redacted)
        self.assertIn("authorization: Bearer [redacted]", redacted)


if __name__ == "__main__":
    unittest.main()
