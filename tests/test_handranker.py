import unittest

from handranker import analyze_postflop, describe_current_hand, evaluate_preflop, simulate_equity


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

    def test_postflop_profile_marks_two_pair_built_on_paired_board(self) -> None:
        profile = analyze_postflop(["Ah", "5h"], ["Jd", "Jc", "2h", "Ac"])

        self.assertEqual(profile.category, "two_pair")
        self.assertEqual(profile.pair_strength, "paired_board_two_pair")

    def test_postflop_profile_detects_four_card_straight_pressure(self) -> None:
        profile = analyze_postflop(["Qh", "9s"], ["2c", "Qs", "Ks", "Jc", "9c"])

        self.assertTrue(profile.board_straight_pressure)

    def test_complete_heads_up_board_uses_exact_equity(self) -> None:
        win, tie, loss = simulate_equity(
            ["Jd", "3d"],
            ["9d", "Qd", "3s", "9h", "4h"],
            player_count=2,
            simulations=1,
        )

        self.assertAlmostEqual(win, 550 / 990)
        self.assertAlmostEqual(tie, 54 / 990)
        self.assertAlmostEqual(loss, 386 / 990)


if __name__ == "__main__":
    unittest.main()
