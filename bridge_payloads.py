from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BridgePayload:
    hole_cards: list[str]
    board_cards: list[str] | None
    players_count: int | None
    reset_state: bool
    hand_id: int | None
    hero_user_id: int | None
    hero_seat_id: int | None
    hero_sitting_out: bool | None


def _parse_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed.isdigit():
            return int(trimmed)
    return None


def parse_bridge_payload(payload: object) -> BridgePayload | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "poker_cards":
        return None

    has_hole = "hole" in payload
    has_board = "board" in payload
    has_players = "players" in payload
    has_reset = "reset" in payload
    has_hand_id = "handId" in payload or "hand_id" in payload
    has_hero_user_id = "heroUserId" in payload or "hero_user_id" in payload
    has_hero_seat_id = "heroSeatId" in payload or "hero_seat_id" in payload
    has_hero_sitting_out = "heroSittingOut" in payload or "hero_sitting_out" in payload
    if not any(
        (
            has_hole,
            has_board,
            has_players,
            has_reset,
            has_hand_id,
            has_hero_user_id,
            has_hero_seat_id,
            has_hero_sitting_out,
        )
    ):
        return None

    hole_cards: list[str] = []
    board_cards: list[str] | None = None
    players_count: int | None = None
    reset_state = False

    if has_hole:
        hole_raw = payload.get("hole")
        if not isinstance(hole_raw, list) or len(hole_raw) != 2:
            return None
        hole_cards = [str(card).strip().upper() for card in hole_raw]

    if has_board:
        board_raw = payload.get("board")
        if not isinstance(board_raw, list) or len(board_raw) > 5:
            return None
        board_cards = [str(card).strip().upper() for card in board_raw]

    if has_players:
        players_raw = payload.get("players")
        if not isinstance(players_raw, int):
            return None
        players_count = max(2, min(10, players_raw))

    if has_reset:
        reset_raw = payload.get("reset")
        if not isinstance(reset_raw, bool):
            return None
        reset_state = reset_raw

    hand_id = payload.get("handId", payload.get("hand_id"))
    hero_user_id = _parse_int(payload.get("heroUserId", payload.get("hero_user_id")))
    hero_seat_raw = payload.get("heroSeatId", payload.get("hero_seat_id"))
    hero_seat_id = hero_seat_raw if isinstance(hero_seat_raw, int) else None
    hero_sitting_out_raw = payload.get("heroSittingOut", payload.get("hero_sitting_out"))
    hero_sitting_out = hero_sitting_out_raw if isinstance(hero_sitting_out_raw, bool) else None

    return BridgePayload(
        hole_cards=hole_cards,
        board_cards=board_cards,
        players_count=players_count,
        reset_state=reset_state,
        hand_id=hand_id if isinstance(hand_id, int) else None,
        hero_user_id=hero_user_id,
        hero_seat_id=hero_seat_id,
        hero_sitting_out=hero_sitting_out,
    )
