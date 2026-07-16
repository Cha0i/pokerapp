from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BridgePayload:
    hole_cards: list[str]
    board_cards: list[str] | None
    players_count: int | None
    reset_state: bool
    hand_id: int | None
    table_id: str | None
    hero_user_id: int | None
    hero_seat_id: int | None
    hero_sitting_out: bool | None
    hero_folded: bool | None
    pot_chips: int | None
    to_call_chips: int | None
    minimum_raise_chips: int | None
    hero_turn: bool | None


_SENSITIVE_LOG_PATTERNS = (
    (re.compile(r"(<auth\b[^>]*>)[\s\S]*?(</auth>)", re.IGNORECASE), r"\1[redacted]\2"),
    (
        re.compile(
            r"(&quot;(?:token|relaxtoken|ticket|password|access_token)&quot;\s*:\s*&quot;)[\s\S]*?(&quot;)",
            re.IGNORECASE,
        ),
        r"\1[redacted]\2",
    ),
    (
        re.compile(r'("(?:token|relaxtoken|ticket|password|access_token)"\s*:\s*")[^"]*(")', re.IGNORECASE),
        r"\1[redacted]\2",
    ),
    (
        re.compile(r"([?&](?:ticket|token|access_token)=)[^&\s|]+", re.IGNORECASE),
        r"\1[redacted]",
    ),
    (
        re.compile(r"(authorization\s*[:=]\s*bearer\s+)[^\s|]+", re.IGNORECASE),
        r"\1[redacted]",
    ),
)


def redact_bridge_log_line(line: str) -> str:
    redacted = line
    for pattern, replacement in _SENSITIVE_LOG_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


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
    has_table_id = "tableId" in payload or "table_id" in payload
    has_hero_user_id = "heroUserId" in payload or "hero_user_id" in payload
    has_hero_seat_id = "heroSeatId" in payload or "hero_seat_id" in payload
    has_hero_sitting_out = "heroSittingOut" in payload or "hero_sitting_out" in payload
    has_hero_folded = "heroFolded" in payload or "hero_folded" in payload
    has_pot = "pot" in payload or "pot_chips" in payload
    has_to_call = "toCall" in payload or "to_call" in payload
    has_minimum_raise = "minimumRaise" in payload or "minimum_raise" in payload
    has_hero_turn = "heroTurn" in payload or "hero_turn" in payload
    if not any(
        (
            has_hole,
            has_board,
            has_players,
            has_reset,
            has_hand_id,
            has_table_id,
            has_hero_user_id,
            has_hero_seat_id,
            has_hero_sitting_out,
            has_hero_folded,
            has_pot,
            has_to_call,
            has_minimum_raise,
            has_hero_turn,
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
    table_id_raw = payload.get("tableId", payload.get("table_id"))
    table_id = None
    if isinstance(table_id_raw, int):
        table_id = str(table_id_raw)
    elif isinstance(table_id_raw, str) and table_id_raw.strip():
        table_id = table_id_raw.strip()
    hero_user_id = _parse_int(payload.get("heroUserId", payload.get("hero_user_id")))
    hero_seat_raw = payload.get("heroSeatId", payload.get("hero_seat_id"))
    hero_seat_id = hero_seat_raw if isinstance(hero_seat_raw, int) else None
    hero_sitting_out_raw = payload.get("heroSittingOut", payload.get("hero_sitting_out"))
    hero_sitting_out = hero_sitting_out_raw if isinstance(hero_sitting_out_raw, bool) else None
    hero_folded_raw = payload.get("heroFolded", payload.get("hero_folded"))
    hero_folded = hero_folded_raw if isinstance(hero_folded_raw, bool) else None
    pot_raw = payload.get("pot", payload.get("pot_chips"))
    pot_chips = pot_raw if isinstance(pot_raw, int) and pot_raw >= 0 else None
    to_call_raw = payload.get("toCall", payload.get("to_call"))
    to_call_chips = to_call_raw if isinstance(to_call_raw, int) and to_call_raw >= 0 else None
    minimum_raise_raw = payload.get("minimumRaise", payload.get("minimum_raise"))
    minimum_raise_chips = (
        minimum_raise_raw
        if isinstance(minimum_raise_raw, int) and minimum_raise_raw >= 0
        else None
    )
    hero_turn_raw = payload.get("heroTurn", payload.get("hero_turn"))
    hero_turn = hero_turn_raw if isinstance(hero_turn_raw, bool) else None

    return BridgePayload(
        hole_cards=hole_cards,
        board_cards=board_cards,
        players_count=players_count,
        reset_state=reset_state,
        hand_id=hand_id if isinstance(hand_id, int) else None,
        table_id=table_id,
        hero_user_id=hero_user_id,
        hero_seat_id=hero_seat_id,
        hero_sitting_out=hero_sitting_out,
        hero_folded=hero_folded,
        pot_chips=pot_chips,
        to_call_chips=to_call_chips,
        minimum_raise_chips=minimum_raise_chips,
        hero_turn=hero_turn,
    )
