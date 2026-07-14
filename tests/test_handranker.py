import unittest

from handranker import describe_current_hand, evaluate_preflop


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


if __name__ == "__main__":
    unittest.main()
