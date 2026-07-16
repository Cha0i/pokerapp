import unittest

from handranker import analyze_postflop, describe_current_hand, evaluate_preflop


class HandRankerTests(unittest.TestCase):
    def test_describes_straight_flush(self) -> None:
        self.assertEqual(
            describe_current_hand(["Ah", "Kh"], ["Qh", "Jh", "Th", "", ""]),
            "Straight flush, Ace high",
        )

    def test_describes_wheel_straight(self) -> None:
        self.assertEqual(
            describe_current_hand(["2c", "3d"], ["4h", "5s", "Ah", "", ""]),
            "Straight, Five high",
        )

    def test_preflop_pair_is_premium(self) -> None:
        result = evaluate_preflop("Ah", "Ad")

        self.assertEqual(result.hand_key, "AA")
        self.assertEqual(result.tier, "Premium")
        self.assertGreaterEqual(result.score, 90)

    def test_postflop_profile_distinguishes_air_from_board_pair(self) -> None:
        profile = analyze_postflop(["Ah", "2s"], ["5h", "Kd", "5d"])

        self.assertEqual(profile.category, "one_pair")
        self.assertEqual(profile.pair_strength, "board_pair")
        self.assertFalse(profile.strong_draw)

    def test_postflop_profile_finds_open_ended_draw(self) -> None:
        profile = analyze_postflop(["7c", "6c"], ["Td", "4s", "5s"])

        self.assertEqual(profile.category, "high_card")
        self.assertEqual(profile.straight_draw_ranks, 2)
        self.assertEqual(profile.draw_outs, 8)
        self.assertAlmostEqual(profile.next_card_draw_equity, 8 / 47)
        self.assertTrue(profile.strong_draw)

    def test_postflop_profile_finds_flush_draw_without_calling_it_value(self) -> None:
        profile = analyze_postflop(["Qs", "6s"], ["8s", "Jd", "9s"])

        self.assertEqual(profile.category, "high_card")
        self.assertTrue(profile.flush_draw)
        self.assertEqual(profile.draw_outs, 12)

    def test_postflop_profile_recognizes_top_pair(self) -> None:
        profile = analyze_postflop(["Qc", "2c"], ["Ts", "5h", "Qd"])

        self.assertEqual(profile.category, "one_pair")
        self.assertEqual(profile.pair_strength, "top_pair")


if __name__ == "__main__":
    unittest.main()
