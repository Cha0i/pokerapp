from __future__ import annotations

import itertools
import random
from collections import Counter
from dataclasses import dataclass
from typing import Sequence


RANKS_DESC = "AKQJT98765432"
SUITS = "cdhs"
RANK_VALUE = {rank: 14 - idx for idx, rank in enumerate(RANKS_DESC)}
DECK = [f"{rank}{suit}" for rank in RANKS_DESC for suit in SUITS]


@dataclass(frozen=True)
class PreflopResult:
    hand_key: str
    hand_name: str
    score: int
    tier: str
    advice: str
    reason: str


PREMIUM = {"AA", "KK", "QQ", "JJ", "AKs"}
STRONG = {"TT", "AKo", "AQs", "AJs", "KQs", "99"}
PLAYABLE = {
    "88",
    "77",
    "AQo",
    "AJo",
    "ATs",
    "KQo",
    "KJs",
    "QJs",
    "JTs",
    "A9s",
    "T9s",
}

RANK_NAMES = {
    "A": "Ace",
    "K": "King",
    "Q": "Queen",
    "J": "Jack",
    "T": "Ten",
    "9": "Nine",
    "8": "Eight",
    "7": "Seven",
    "6": "Six",
    "5": "Five",
    "4": "Four",
    "3": "Three",
    "2": "Two",
}

RANK_PLURALS = {
    "A": "Aces",
    "K": "Kings",
    "Q": "Queens",
    "J": "Jacks",
    "T": "Tens",
    "9": "Nines",
    "8": "Eights",
    "7": "Sevens",
    "6": "Sixes",
    "5": "Fives",
    "4": "Fours",
    "3": "Threes",
    "2": "Twos",
}

HAND_CATEGORY_ORDER = [
    ("straight_flush", "Straight flush"),
    ("four_kind", "4 of a Kind"),
    ("full_house", "Full house"),
    ("flush", "Flush"),
    ("straight", "Straight"),
    ("three_kind", "3-of-a-Kind"),
    ("two_pair", "2 pair"),
    ("one_pair", "1 pair"),
    ("high_card", "High card"),
]

CATEGORY_VALUE_TO_KEY = {
    8: "straight_flush",
    7: "four_kind",
    6: "full_house",
    5: "flush",
    4: "straight",
    3: "three_kind",
    2: "two_pair",
    1: "one_pair",
    0: "high_card",
}


def _normalize_cards(card_a: str, card_b: str) -> tuple[str, str, str, str]:
    if len(card_a) != 2 or len(card_b) != 2:
        raise ValueError("Cards must be 2 characters each, for example 'Ah' or 'Tc'.")
    rank_a, suit_a = card_a[0].upper(), card_a[1].lower()
    rank_b, suit_b = card_b[0].upper(), card_b[1].lower()

    if rank_a not in RANK_VALUE or rank_b not in RANK_VALUE:
        raise ValueError("Invalid rank. Use one of A,K,Q,J,T,9..2.")
    if suit_a not in SUITS or suit_b not in SUITS:
        raise ValueError("Invalid suit. Use one of c,d,h,s.")
    if rank_a == rank_b and suit_a == suit_b:
        raise ValueError("You cannot select the same exact card twice.")
    return rank_a, suit_a, rank_b, suit_b


def _hand_key(rank_a: str, suit_a: str, rank_b: str, suit_b: str) -> str:
    if rank_a == rank_b:
        return f"{rank_a}{rank_b}"

    high, low = sorted([rank_a, rank_b], key=lambda r: RANK_VALUE[r], reverse=True)
    suited = suit_a == suit_b
    return f"{high}{low}{'s' if suited else 'o'}"


def _score_from_structure(rank_a: str, suit_a: str, rank_b: str, suit_b: str) -> tuple[int, str]:
    value_a = RANK_VALUE[rank_a]
    value_b = RANK_VALUE[rank_b]
    high = max(value_a, value_b)
    low = min(value_a, value_b)
    is_pair = rank_a == rank_b
    is_suited = suit_a == suit_b
    gap = high - low

    score = 0
    reasons: list[str] = []

    if is_pair:
        pair_score = {
            14: 98,
            13: 96,
            12: 92,
            11: 88,
            10: 82,
            9: 76,
            8: 70,
            7: 63,
            6: 58,
            5: 54,
            4: 50,
            3: 47,
            2: 44,
        }
        score = pair_score[high]
        reasons.append("Pocket pair")
    else:
        score += high * 4
        score += low * 2
        reasons.append("High-card strength")

        if is_suited:
            score += 6
            reasons.append("Suited")

        if gap == 1:
            score += 5
            reasons.append("Connected")
        elif gap == 2:
            score += 2
            reasons.append("One-gapper")
        elif gap >= 4:
            score -= 4
            reasons.append("Large gap")

        if high >= 13 and low >= 10:
            score += 6
            reasons.append("Broadway combo")

    return max(0, min(100, score)), ", ".join(reasons)


def _describe_hole_hand(rank_a: str, suit_a: str, rank_b: str, suit_b: str) -> str:
    if rank_a == rank_b:
        return f"Pair of {RANK_PLURALS[rank_a]}"

    ordered = sorted([rank_a, rank_b], key=lambda rank: RANK_VALUE[rank], reverse=True)
    suited_text = "suited" if suit_a == suit_b else "offsuit"
    if abs(RANK_VALUE[rank_a] - RANK_VALUE[rank_b]) == 1:
        shape = "connectors"
    elif abs(RANK_VALUE[rank_a] - RANK_VALUE[rank_b]) == 2:
        shape = "one-gapper"
    else:
        shape = "high-card hand" if RANK_VALUE[ordered[0]] >= 11 else "unpaired hand"
    return f"{RANK_NAMES[ordered[0]]}-{RANK_NAMES[ordered[1]]} {suited_text} {shape}"


def _build_advice(key: str, score: int, tier: str, rank_a: str, suit_a: str, rank_b: str, suit_b: str) -> str:
    is_pair = rank_a == rank_b
    is_suited = suit_a == suit_b
    gap = abs(RANK_VALUE[rank_a] - RANK_VALUE[rank_b])
    high_rank = max(rank_a, rank_b, key=lambda rank: RANK_VALUE[rank])

    position_note = "From early position stay disciplined; from late position you can press thinner edges."
    multiway_note = "It plays worse against many callers than its raw score suggests." if not is_pair and gap >= 3 else "It keeps its value well when stacks go in pre-flop."

    if tier == "Premium":
        return (
            "Open-raise and usually continue aggressively against a 3-bet. "
            "You are mainly targeting value, not trying to realize equity cheaply. "
            f"{multiway_note}"
        )
    if tier == "Strong":
        return (
            "This is strong enough to raise first in and often continue versus pressure, "
            "but table action and position matter more than with premiums. "
            f"{position_note}"
        )
    if tier == "Playable":
        draw_note = "It gains value from making strong top pair or strong draws." if is_suited or high_rank in {"A", "K", "Q"} else "It prefers seeing flops at reasonable cost."
        return (
            "Usually open from middle or late position and be more selective facing raises. "
            f"{draw_note} {position_note}"
        )
    if tier == "Speculative":
        connector_note = "It benefits from hidden straights and flushes." if is_suited or gap <= 2 else "It rarely makes nutted hands often enough to build big pots early."
        return (
            "Treat this as a situation-dependent hand: better in late position, better deep stacked, and better when you can see a flop cheaply. "
            f"{connector_note} Avoid bloating the pot before the flop."
        )
    fold_note = "The pair is too small to force action without good implied odds." if is_pair else "Its reverse-implied-odds risk is high when dominated."
    return (
        "Default to folding this hand unless you have a very specific reason to continue, such as a free option in the blinds or unusually passive action. "
        f"{fold_note}"
    )


def evaluate_preflop(card_a: str, card_b: str) -> PreflopResult:
    rank_a, suit_a, rank_b, suit_b = _normalize_cards(card_a, card_b)
    key = _hand_key(rank_a, suit_a, rank_b, suit_b)
    hand_name = _describe_hole_hand(rank_a, suit_a, rank_b, suit_b)
    score, reason = _score_from_structure(rank_a, suit_a, rank_b, suit_b)

    if key in PREMIUM:
        return PreflopResult(
            hand_key=key,
            hand_name=hand_name,
            score=max(score, 90),
            tier="Premium",
            advice=_build_advice(key, max(score, 90), "Premium", rank_a, suit_a, rank_b, suit_b),
            reason=reason,
        )
    if key in STRONG:
        return PreflopResult(
            hand_key=key,
            hand_name=hand_name,
            score=max(score, 78),
            tier="Strong",
            advice=_build_advice(key, max(score, 78), "Strong", rank_a, suit_a, rank_b, suit_b),
            reason=reason,
        )
    if key in PLAYABLE:
        return PreflopResult(
            hand_key=key,
            hand_name=hand_name,
            score=max(score, 62),
            tier="Playable",
            advice=_build_advice(key, max(score, 62), "Playable", rank_a, suit_a, rank_b, suit_b),
            reason=reason,
        )

    if score >= 75:
        tier = "Strong"
        advice = "Usually raise; value hand pre-flop"
    elif score >= 60:
        tier = "Playable"
        advice = "Play in good position or with favorable action"
    elif score >= 45:
        tier = "Speculative"
        advice = "Mostly call in position; avoid big pots"
    else:
        tier = "Fold"
        advice = "Usually fold pre-flop"

    return PreflopResult(
        hand_key=key,
        hand_name=hand_name,
        score=score,
        tier=tier,
        advice=_build_advice(key, score, tier, rank_a, suit_a, rank_b, suit_b),
        reason=reason,
    )


def _parse_card(card: str) -> tuple[int, str]:
    return RANK_VALUE[card[0]], card[1]


def _straight_high(ranks: Sequence[int]) -> int | None:
    unique = set(ranks)
    if 14 in unique:
        unique.add(1)
    ordered = sorted(unique)
    run = 1
    best: int | None = None
    for prev, cur in zip(ordered, ordered[1:]):
        if cur == prev + 1:
            run += 1
            if run >= 5:
                best = cur
        else:
            run = 1
    return best


def _evaluate_five(cards: Sequence[str]) -> tuple[int, tuple[int, ...]]:
    parsed = [_parse_card(card) for card in cards]
    ranks = sorted((rank for rank, _ in parsed), reverse=True)
    suits = [suit for _, suit in parsed]
    counts = Counter(ranks)
    by_count = sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)
    is_flush = len(set(suits)) == 1
    straight = _straight_high(ranks)

    if is_flush and straight is not None:
        return 8, (straight,)
    if by_count[0][1] == 4:
        four = by_count[0][0]
        kicker = max(rank for rank in ranks if rank != four)
        return 7, (four, kicker)
    if by_count[0][1] == 3 and by_count[1][1] == 2:
        return 6, (by_count[0][0], by_count[1][0])
    if is_flush:
        return 5, tuple(ranks)
    if straight is not None:
        return 4, (straight,)
    if by_count[0][1] == 3:
        trips = by_count[0][0]
        kickers = tuple(sorted((rank for rank in ranks if rank != trips), reverse=True))
        return 3, (trips, *kickers)

    pairs = [rank for rank, count in by_count if count == 2]
    if len(pairs) >= 2:
        top, second = sorted(pairs, reverse=True)[:2]
        kicker = max(rank for rank in ranks if rank not in {top, second})
        return 2, (top, second, kicker)
    if len(pairs) == 1:
        pair = pairs[0]
        kickers = tuple(sorted((rank for rank in ranks if rank != pair), reverse=True))
        return 1, (pair, *kickers)
    return 0, tuple(ranks)


def _best_hand(cards: Sequence[str]) -> tuple[int, tuple[int, ...]]:
    return max(_evaluate_five(combo) for combo in itertools.combinations(cards, 5))


def _high_card_name(rank: int) -> str:
    for symbol, value in RANK_VALUE.items():
        if value == rank:
            return RANK_NAMES[symbol]
    raise ValueError(f"Unknown rank value: {rank}")


def _plural_name(rank: int) -> str:
    for symbol, value in RANK_VALUE.items():
        if value == rank:
            return RANK_PLURALS[symbol]
    raise ValueError(f"Unknown rank value: {rank}")


def describe_current_hand(hero_cards: Sequence[str], board_cards: Sequence[str]) -> str:
    known_board = [card for card in board_cards if card]
    cards = [*hero_cards, *known_board]
    if len(cards) < 2:
        return "-"

    ranks = sorted((_parse_card(card)[0] for card in cards), reverse=True)
    counts = Counter(ranks)
    ordered_counts = sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)
    pairs = sorted((rank for rank, count in counts.items() if count == 2), reverse=True)

    if len(cards) < 5:
        if ordered_counts[0][1] == 4:
            return f"Four of a kind, {_plural_name(ordered_counts[0][0])}"
        if ordered_counts[0][1] == 3 and len(pairs) >= 1:
            return f"Full house, {_plural_name(ordered_counts[0][0])} over {_plural_name(pairs[0])}"
        if ordered_counts[0][1] == 3:
            return f"Three of a kind, {_plural_name(ordered_counts[0][0])}"
        if len(pairs) >= 2:
            return f"Two pair, {_plural_name(pairs[0])} and {_plural_name(pairs[1])}"
        if len(pairs) == 1:
            return f"Pair of {_plural_name(pairs[0])}"
        return f"{_high_card_name(ranks[0])} high"

    category, detail = _best_hand(cards)
    if category == 8:
        return f"Straight flush, {_high_card_name(detail[0])} high"
    if category == 7:
        return f"Four of a kind, {_plural_name(detail[0])}"
    if category == 6:
        return f"Full house, {_plural_name(detail[0])} over {_plural_name(detail[1])}"
    if category == 5:
        return f"Flush, {_high_card_name(detail[0])} high"
    if category == 4:
        return f"Straight, {_high_card_name(detail[0])} high"
    if category == 3:
        return f"Three of a kind, {_plural_name(detail[0])}"
    if category == 2:
        return f"Two pair, {_plural_name(detail[0])} and {_plural_name(detail[1])}"
    if category == 1:
        return f"Pair of {_plural_name(detail[0])}"
    return f"{_high_card_name(detail[0])} high"


def simulate_equity(
    hero_cards: Sequence[str],
    board_cards: Sequence[str],
    player_count: int,
    simulations: int = 5000,
) -> tuple[float, float, float]:
    if len(hero_cards) != 2:
        raise ValueError("Hero cards must contain exactly two cards.")
    if len(board_cards) != 5:
        raise ValueError("Board cards must have exactly five slots.")
    if player_count < 2:
        raise ValueError("Player count must be at least 2.")

    known_board = [card for card in board_cards if card]
    used = list(hero_cards) + known_board
    if len(set(used)) != len(used):
        raise ValueError("Duplicate card detected in hero or board cards.")

    remaining = [card for card in DECK if card not in used]
    unknown_board = 5 - len(known_board)
    opponents = player_count - 1
    cards_needed = unknown_board + opponents * 2
    if cards_needed > len(remaining):
        raise ValueError("Not enough cards remaining for the requested simulation.")

    wins = 0.0
    ties = 0.0

    for _ in range(simulations):
        draw = random.sample(remaining, cards_needed)
        draw_idx = 0

        final_board: list[str] = []
        for card in board_cards:
            if card:
                final_board.append(card)
            else:
                final_board.append(draw[draw_idx])
                draw_idx += 1

        villain_hands: list[list[str]] = []
        for _opp in range(opponents):
            villain_hands.append([draw[draw_idx], draw[draw_idx + 1]])
            draw_idx += 2

        hero_score = _best_hand([*hero_cards, *final_board])
        villain_scores = [_best_hand([*hand, *final_board]) for hand in villain_hands]
        all_scores = [hero_score, *villain_scores]
        best = max(all_scores)
        winners = sum(score == best for score in all_scores)

        if hero_score == best and winners == 1:
            wins += 1.0
        elif hero_score == best:
            ties += 1.0 / winners

    win_rate = wins / simulations
    tie_rate = ties / simulations
    loss_rate = max(0.0, 1.0 - win_rate - tie_rate)
    return win_rate, tie_rate, loss_rate


def simulate_hand_rank_distribution(
    hero_cards: Sequence[str],
    board_cards: Sequence[str],
    simulations: int = 3000,
) -> tuple[dict[str, float], dict[str, float]]:
    if len(hero_cards) != 2:
        raise ValueError("Hero cards must contain exactly two cards.")
    if len(board_cards) != 5:
        raise ValueError("Board cards must have exactly five slots.")

    known_board = [card for card in board_cards if card]
    used = list(hero_cards) + known_board
    if len(set(used)) != len(used):
        raise ValueError("Duplicate card detected in hero or board cards.")

    remaining = [card for card in DECK if card not in used]
    unknown_board = 5 - len(known_board)
    cards_needed = unknown_board + 2
    if cards_needed > len(remaining):
        raise ValueError("Not enough cards remaining for the requested simulation.")

    hero_counts = Counter({key: 0 for key, _label in HAND_CATEGORY_ORDER})
    other_counts = Counter({key: 0 for key, _label in HAND_CATEGORY_ORDER})

    for _ in range(simulations):
        draw = random.sample(remaining, cards_needed)
        draw_idx = 0

        final_board: list[str] = []
        for card in board_cards:
            if card:
                final_board.append(card)
            else:
                final_board.append(draw[draw_idx])
                draw_idx += 1

        other_cards = [draw[draw_idx], draw[draw_idx + 1]]

        hero_category = _best_hand([*hero_cards, *final_board])[0]
        other_category = _best_hand([*other_cards, *final_board])[0]
        hero_counts[CATEGORY_VALUE_TO_KEY[hero_category]] += 1
        other_counts[CATEGORY_VALUE_TO_KEY[other_category]] += 1

    hero_rates = {key: hero_counts[key] / simulations for key, _label in HAND_CATEGORY_ORDER}
    other_rates = {key: other_counts[key] / simulations for key, _label in HAND_CATEGORY_ORDER}
    return hero_rates, other_rates