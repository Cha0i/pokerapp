#!/usr/bin/env python3
"""Correlate PokerOdds advice with actions and outcomes from Unibet logs."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any, Iterable


BODY_RE = re.compile(r"<body\b[^>]*>([\s\S]*?)</body>", re.IGNORECASE)
STREETS = ("preflop", "flop", "turn", "river")


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_json_lines(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
            if isinstance(value, dict):
                yield value


def parse_frame_bodies(raw_line: object) -> list[dict[str, Any]]:
    if not isinstance(raw_line, str) or "wspoker" not in raw_line or "<body" not in raw_line:
        return []
    bodies: list[dict[str, Any]] = []
    for match in BODY_RE.finditer(raw_line):
        try:
            body = json.loads(html.unescape(match.group(1)))
        except json.JSONDecodeError:
            continue
        if isinstance(body, dict):
            bodies.append(body)
    return bodies


@dataclass
class Advice:
    ts: datetime
    hand_id: int
    street: str
    players: int | None
    recommendation: str
    headline: str
    hole: tuple[str, ...]
    board: tuple[str, ...]
    pot: int | None
    to_call: int | None
    random_equity: float | None
    used: bool = False


@dataclass
class Decision:
    ts: datetime
    hand_id: int
    action: str
    cost: int
    advice: Advice | None = None


@dataclass
class Hand:
    hand_id: int
    table_id: int | None = None
    hero_seat: int | None = None
    hole: tuple[str, ...] = ()
    board: tuple[str, ...] = ()
    start_stack: int | None = None
    end_stack: int | None = None
    decisions: list[Decision] = field(default_factory=list)

    @property
    def delta(self) -> int | None:
        if self.start_stack is None or self.end_stack is None:
            return None
        return self.end_stack - self.start_stack


def compact_cards(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or len(value) % 2:
        return ()
    cards: list[str] = []
    for index in range(0, len(value), 2):
        card = value[index : index + 2]
        if not re.fullmatch(r"[2-9TJQKA][cdhs]", card, re.IGNORECASE):
            return ()
        cards.append(card[0].upper() + card[1].lower())
    return tuple(cards)


def hero_seat(compact_payload: dict[str, Any], hero_name: str) -> int | None:
    compact_table = compact_payload.get("c")
    if isinstance(compact_table, list) and compact_table and isinstance(compact_table[0], str):
        names = [name.strip().lower() for name in compact_table[0].split("|")]
        try:
            return names.index(hero_name.lower())
        except ValueError:
            pass
    player = compact_payload.get("p")
    if isinstance(player, list) and len(player) > 1 and isinstance(player[1], int):
        return player[1]
    return None


def stack_at(compact_payload: dict[str, Any], seat: int | None) -> int | None:
    compact_table = compact_payload.get("c")
    if seat is None or not isinstance(compact_table, list) or len(compact_table) <= 2:
        return None
    stacks = compact_table[2]
    if not isinstance(stacks, list) or not 0 <= seat < len(stacks) or not isinstance(stacks[seat], int):
        return None
    return stacks[seat]


def load_advice(path: Path) -> dict[int, list[Advice]]:
    by_hand: dict[int, list[Advice]] = defaultdict(list)
    for event in read_json_lines(path):
        if event.get("event") != "advice_snapshot":
            continue
        ts = parse_timestamp(event.get("ts"))
        hand_id = event.get("hand_id")
        street = event.get("street")
        if ts is None or not isinstance(hand_id, int) or street not in STREETS:
            continue
        by_hand[hand_id].append(
            Advice(
                ts=ts,
                hand_id=hand_id,
                street=street,
                players=event.get("players") if isinstance(event.get("players"), int) else None,
                recommendation=str(event.get("advice_recommendation", "unknown")),
                headline=str(event.get("advice_headline", "")),
                hole=tuple(str(card) for card in event.get("hole", []) if isinstance(card, str)),
                board=tuple(str(card) for card in event.get("board", []) if isinstance(card, str)),
                pot=event.get("pot") if isinstance(event.get("pot"), int) else None,
                to_call=event.get("to_call") if isinstance(event.get("to_call"), int) else None,
                random_equity=(
                    float(event["random_equity"])
                    if isinstance(event.get("random_equity"), (int, float))
                    else None
                ),
            )
        )
    for advice in by_hand.values():
        advice.sort(key=lambda item: item.ts)
    return by_hand


def load_hands(path: Path, hero_name: str) -> dict[int, Hand]:
    hands: dict[int, Hand] = {}
    for event in read_json_lines(path):
        if event.get("event") != "bridge_line_received":
            continue
        ts = parse_timestamp(event.get("ts"))
        raw_line = event.get("raw_line")
        if ts is None or not isinstance(raw_line, str):
            continue
        direction_send = ":WS_SEND]" in raw_line
        direction_message = ":WS_MESSAGE]" in raw_line
        if not direction_send and not direction_message:
            continue
        for body in parse_frame_bodies(raw_line):
            if direction_send:
                action = body.get("action")
                params = body.get("params")
                if action not in {"fold", "check", "call", "bet", "raise", "allin"} or not isinstance(params, dict):
                    continue
                hand_id = params.get("hid")
                if not isinstance(hand_id, int):
                    continue
                cost = params.get("cost")
                hands.setdefault(hand_id, Hand(hand_id)).decisions.append(
                    Decision(ts, hand_id, str(action), cost if isinstance(cost, int) else 0)
                )
                continue

            tags = body.get("tags")
            payload = body.get("payLoad")
            if not isinstance(tags, list) or not isinstance(payload, dict):
                continue
            hand_id = payload.get("hid")
            if not isinstance(hand_id, int):
                continue
            hand = hands.setdefault(hand_id, Hand(hand_id))
            seat = hero_seat(payload, hero_name)
            if seat is not None:
                hand.hero_seat = seat
            table_id = payload.get("tid")
            if isinstance(table_id, int):
                hand.table_id = table_id
            player = payload.get("p")
            if isinstance(player, list) and len(player) > 3:
                hole = compact_cards(player[3])
                if len(hole) == 2:
                    hand.hole = hole
            compact_table = payload.get("c")
            if isinstance(compact_table, list) and len(compact_table) > 7:
                board = compact_cards(compact_table[7])
                if 3 <= len(board) <= 5:
                    hand.board = board
            stack = stack_at(payload, hand.hero_seat)
            tag_set = {str(tag) for tag in tags}
            if stack is not None and hand.start_stack is None and "finished" not in tag_set:
                hand.start_stack = stack
            if stack is not None and "finished" in tag_set and "winner" in tag_set:
                hand.end_stack = stack
    for hand in hands.values():
        hand.decisions.sort(key=lambda item: item.ts)
    return hands


def match_advice(hands: dict[int, Hand], advice_by_hand: dict[int, list[Advice]]) -> None:
    for hand_id, hand in hands.items():
        advice = advice_by_hand.get(hand_id, [])
        for decision in hand.decisions:
            available = [item for item in advice if not item.used and item.ts <= decision.ts]
            if not available:
                continue
            decision.advice = available[-1]
            decision.advice.used = True
            if not hand.hole:
                hand.hole = decision.advice.hole


def action_matches(advice: str, action: str) -> bool:
    accepted = {
        "fold": {"fold"},
        "check": {"check"},
        "call": {"call"},
        "bet": {"bet", "raise"},
        "raise": {"bet", "raise"},
        "call_or_raise": {"call", "bet", "raise"},
    }
    return action in accepted.get(advice, {advice})


def money(value: int | None) -> str:
    return "?" if value is None else f"{value:+d}"


def summarize(hands: dict[int, Hand], advice_by_hand: dict[int, list[Advice]]) -> str:
    # Raw Relax/XMPP hands are the authoritative site boundary. Advice logs can
    # also contain ReplayPoker hands when both supported sites were open.
    strategy_ids = set(advice_by_hand).intersection(hands)
    strategy_hands = [hands.setdefault(hand_id, Hand(hand_id)) for hand_id in sorted(strategy_ids)]
    complete = [hand for hand in strategy_hands if hand.delta is not None]
    decisions = [decision for hand in strategy_hands for decision in hand.decisions if decision.advice is not None]
    unmatched_actions = sum(len(hand.decisions) for hand in strategy_hands) - len(decisions)
    unused_advice = sum(
        not item.used
        for hand_id in strategy_ids
        for item in advice_by_hand.get(hand_id, [])
    )

    action_counts = Counter(decision.action for decision in decisions)
    recommendation_counts = Counter(decision.advice.recommendation for decision in decisions if decision.advice)
    ignored_folds = [
        decision
        for decision in decisions
        if decision.advice and decision.advice.recommendation == "fold" and decision.action != "fold"
    ]
    followed_folds = [
        decision
        for decision in decisions
        if decision.advice and decision.advice.recommendation == "fold" and decision.action == "fold"
    ]
    call_over_folds = [decision for decision in ignored_folds if decision.action == "call"]
    ignored_fold_hands = {decision.hand_id for decision in ignored_folds}
    ignored_complete = [hand for hand in complete if hand.hand_id in ignored_fold_hands]
    strict_complete = [hand for hand in complete if hand.hand_id not in ignored_fold_hands]

    preflop_decisions = [decision for decision in decisions if decision.advice and decision.advice.street == "preflop"]
    preflop_hands = {decision.hand_id for decision in preflop_decisions}
    vpip_hands = {
        decision.hand_id
        for decision in preflop_decisions
        if decision.action in {"call", "bet", "raise", "allin"}
    }
    pfr_hands = {
        decision.hand_id
        for decision in preflop_decisions
        if decision.action in {"bet", "raise", "allin"}
    }
    postflop_bets = [
        decision
        for decision in decisions
        if decision.advice
        and decision.advice.street != "preflop"
        and decision.action == "bet"
        and decision.cost > 0
        and isinstance(decision.advice.pot, int)
        and decision.advice.pot > 0
    ]
    overbet_hand_ids = {
        decision.hand_id
        for decision in postflop_bets
        if decision.cost > (decision.advice.pot or 0)
    }
    overbet_hands = [hand for hand in complete if hand.hand_id in overbet_hand_ids]
    other_bet_hands = [hand for hand in complete if hand.hand_id not in overbet_hand_ids]
    call_over_fold_ids = {
        decision.hand_id
        for decision in call_over_folds
    }
    call_over_fold_results = [
        hand.delta
        for hand in complete
        if hand.hand_id in call_over_fold_ids and hand.delta is not None
    ]
    vpip_by_players: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for hand_id in preflop_hands:
        first = next(
            (
                decision
                for decision in hands[hand_id].decisions
                if decision.advice and decision.advice.street == "preflop"
            ),
            None,
        )
        if first is None or first.advice is None or first.advice.players is None:
            continue
        bucket = vpip_by_players[first.advice.players]
        bucket[1] += 1
        if hand_id in vpip_hands:
            bucket[0] += 1

    net = sum(hand.delta or 0 for hand in complete)
    lines = [
        "UNIBET SESSION ANALYSIS",
        f"Strategy hands: {len(strategy_hands)} ({len(complete)} with complete stack outcomes)",
        f"Matched decisions: {len(decisions)}; unmatched actions: {unmatched_actions}; unused advice snapshots: {unused_advice}",
        f"Net stack change across complete strategy hands: {money(net)}",
        f"Actions: {dict(sorted(action_counts.items()))}",
        f"Advice: {dict(sorted(recommendation_counts.items()))}",
    ]
    if preflop_hands:
        lines.append(
            f"Observed VPIP: {len(vpip_hands)}/{len(preflop_hands)} ({len(vpip_hands) / len(preflop_hands):.1%}); "
            f"PFR: {len(pfr_hands)}/{len(preflop_hands)} ({len(pfr_hands) / len(preflop_hands):.1%})"
        )
    if postflop_bets:
        fractions = [decision.cost / (decision.advice.pot or 1) for decision in postflop_bets]
        lines.append(
            f"Postflop bet size: median {median(fractions):.0%} pot; "
            f">75% pot {sum(value > 0.75 for value in fractions)}/{len(fractions)}; "
            f"overbets {sum(value > 1.0 for value in fractions)}/{len(fractions)}"
        )
        lines.append(
            f"Hands containing an overbet: {len(overbet_hands)}, "
            f"won {sum((hand.delta or 0) > 0 for hand in overbet_hands)}, "
            f"net {money(sum(hand.delta or 0 for hand in overbet_hands))}; "
            f"other hands net {money(sum(hand.delta or 0 for hand in other_bet_hands))}"
        )
        lines.append(
            "VPIP by active players: "
            + ", ".join(
                f"{players}P {played}/{total} ({played / total:.0%})"
                for players, (played, total) in sorted(vpip_by_players.items())
            )
        )
    lines.extend(
        [
            f"Fold advice followed: {len(followed_folds)}; ignored: {len(ignored_folds)} "
            f"({len(call_over_folds)} calls)",
            (
                f"Hands containing ignored fold advice: {len(ignored_complete)}, "
                f"won {sum((hand.delta or 0) > 0 for hand in ignored_complete)}, "
                f"net {money(sum(hand.delta or 0 for hand in ignored_complete))}"
            ),
            (
                f"Other complete hands: {len(strict_complete)}, "
                f"won {sum((hand.delta or 0) > 0 for hand in strict_complete)}, "
                f"net {money(sum(hand.delta or 0 for hand in strict_complete))}"
            ),
            "",
            "IGNORED FOLD ADVICE BY HAND",
        ]
    )
    if len(call_over_fold_results) >= 2:
        standard_error = stdev(call_over_fold_results) / (len(call_over_fold_results) ** 0.5)
        low = mean(call_over_fold_results) - (1.96 * standard_error)
        high = mean(call_over_fold_results) + (1.96 * standard_error)
        lines.insert(
            lines.index("IGNORED FOLD ADVICE BY HAND") - 1,
            f"Call-over-fold hands averaged {mean(call_over_fold_results):+.1f}; "
            f"rough 95% interval {low:+.1f} to {high:+.1f} (not enough evidence of positive EV)",
        )

    for hand in sorted(ignored_complete, key=lambda item: item.delta or 0, reverse=True):
        disagreements = [decision for decision in hand.decisions if decision in ignored_folds]
        descriptions = []
        for decision in disagreements:
            assert decision.advice is not None
            descriptions.append(
                f"{decision.advice.street}:{decision.action} cost={decision.cost} "
                f"pot={decision.advice.pot} to_call={decision.advice.to_call}"
            )
        lines.append(
            f"{hand.hand_id} {' '.join(hand.hole) or '?'} delta={money(hand.delta)} | " + "; ".join(descriptions)
        )

    lines.extend(["", "BIGGEST COMPLETE-HAND OUTCOMES"])
    extremes = sorted(complete, key=lambda item: item.delta or 0)[:8]
    extremes += sorted(complete, key=lambda item: item.delta or 0, reverse=True)[:8]
    for hand in extremes:
        decisions_text = []
        for decision in hand.decisions:
            if decision.advice is None:
                continue
            decisions_text.append(
                f"{decision.advice.street}:{decision.advice.recommendation}->{decision.action}({decision.cost})"
            )
        lines.append(
            f"{hand.hand_id} {' '.join(hand.hole) or '?'} delta={money(hand.delta)} | "
            + ", ".join(decisions_text)
        )

    lines.extend(["", "DECISION CONFUSION"])
    confusion = Counter(
        (decision.advice.recommendation, decision.action)
        for decision in decisions
        if decision.advice is not None
    )
    for (recommendation, action), count in sorted(confusion.items()):
        lines.append(f"{recommendation:13s} -> {action:6s}: {count}")

    matched = sum(action_matches(decision.advice.recommendation, decision.action) for decision in decisions if decision.advice)
    lines.append(f"Exact/compatible advice agreement: {matched}/{len(decisions)} ({matched / len(decisions):.1%})" if decisions else "No matched decisions")
    return "\n".join(lines)


def recommendation_from_headline(headline: str) -> str:
    normalized = headline.upper()
    if "FOLD" in normalized:
        return "fold"
    if "CALL" in normalized and "RAISE" in normalized:
        return "call_or_raise"
    if "CALL" in normalized:
        return "call"
    if "BET" in normalized:
        return "bet"
    if "RAISE" in normalized:
        return "raise"
    if "CHECK" in normalized:
        return "check"
    return "mixed"


def replay_current_postflop_strategy(path: Path) -> str:
    repo_root = str(Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from app import _recommend_facing_postflop_bet, _recommend_when_checked_to
    from handranker import analyze_postflop

    changes: list[str] = []
    postflop = 0
    for event in read_json_lines(path):
        if event.get("event") != "advice_snapshot":
            continue
        hand_id = event.get("hand_id")
        street = event.get("street")
        hole = event.get("hole")
        board = event.get("board")
        if not isinstance(hand_id, int) or street not in STREETS[1:]:
            continue
        if not isinstance(hole, list) or len(hole) != 2 or not isinstance(board, list) or len(board) < 3:
            continue
        profile = analyze_postflop(hole, board)
        to_call = event.get("to_call")
        pot = event.get("pot")
        equity = event.get("random_equity")
        players = event.get("players")
        if isinstance(to_call, int) and to_call > 0 and isinstance(pot, int) and pot + to_call > 0:
            headline, _reason, _target = _recommend_facing_postflop_bet(
                profile,
                float(equity) if isinstance(equity, (int, float)) else None,
                to_call / (pot + to_call),
                players if isinstance(players, int) else 2,
            )
        else:
            headline, _reason = _recommend_when_checked_to(profile, street)
        postflop += 1
        old = str(event.get("advice_recommendation", "unknown"))
        new = recommendation_from_headline(headline)
        if old != new or str(event.get("advice_headline", "")) != headline:
            changes.append(f"{hand_id} {street}: {old} -> {new} | {headline}")
    return "\n".join(
        [
            "CURRENT STRATEGY REPLAY",
            f"Postflop snapshots replayed: {postflop}; changed headlines: {len(changes)}",
            *changes,
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-log", type=Path, default=Path("app-actions.log"))
    parser.add_argument("--strategy-log", type=Path, default=Path("strategy-training.log"))
    parser.add_argument("--hero", default="xtlx")
    parser.add_argument("--replay-current", action="store_true")
    args = parser.parse_args()

    advice = load_advice(args.strategy_log)
    hands = load_hands(args.app_log, args.hero)
    match_advice(hands, advice)
    print(summarize(hands, advice))
    if args.replay_current:
        print()
        print(replay_current_postflop_strategy(args.strategy_log))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
