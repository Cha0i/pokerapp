from __future__ import annotations

import json
import html
import re
import sqlite3
import threading
import sys
import traceback
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue

try:
    from flask import Flask, request
    from werkzeug.serving import make_server
    _BRIDGE_DEPENDENCIES_AVAILABLE = True
except ImportError:
    Flask = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]
    make_server = None  # type: ignore[assignment]
    _BRIDGE_DEPENDENCIES_AVAILABLE = False

from bridge_payloads import parse_bridge_payload, redact_bridge_log_line
from handranker import (
    HAND_CATEGORY_ORDER,
    PostflopProfile,
    RANKS_DESC,
    analyze_postflop,
    describe_current_hand,
    evaluate_preflop,
    simulate_equity,
    simulate_hand_rank_distribution,
)


DISPLAY_SUITS = ("h", "d", "c", "s")
SUIT_SYMBOLS = {
    "h": "\u2665",
    "d": "\u2666",
    "c": "\u2663",
    "s": "\u2660",
}

BG_MAIN = "#121418"
BG_PANEL = "#1a1f27"
BG_BUTTON = "#232a35"
BG_BUTTON_SELECTED = "#2f4b66"
BG_BUTTON_HOLE_SELECTED = "#1f7a3f"
BG_BUTTON_TURN_SELECTED = "#9f1239"
BG_BUTTON_RIVER_SELECTED = "#1d4ed8"
BG_BUTTON_LOCKED = "#2a2f3a"
FG_MAIN = "#e8edf3"
FG_MUTED = "#aab4c3"
FG_RED = "#ff5f57"
FG_LIGHT = "#d9e2ec"
BRIDGE_TAG = "TM_BRIDGE:"

BASE_FONT_SIZES = {
    "title": 24,
    "subtitle": 13,
    "section": 16,
    "card": 15,
    "label_large": 18,
    "label": 13,
    "small": 11,
    "window": 11,
}
BOARD_ORDER = ["flop_1", "flop_2", "flop_3", "turn", "river"]
BOARD_LABELS = {
    "flop_1": "Flop 1",
    "flop_2": "Flop 2",
    "flop_3": "Flop 3",
    "turn": "Turn",
    "river": "River",
}


@dataclass(frozen=True)
class Card:
    rank: str
    suit: str

    @property
    def code(self) -> str:
        return f"{self.rank}{self.suit}"


@dataclass(frozen=True)
class BridgeSite:
    key: str
    label: str
    url: str
    tracker_site: str
    allowed_origins: tuple[str, ...]


def _range_adjusted_equity_target(pot_odds: float, players: int) -> float:
    buffer = 0.06
    if pot_odds >= 0.25:
        buffer += 0.04
    if pot_odds >= 0.33:
        buffer += 0.04
    if players >= 3:
        buffer += 0.04
    return min(0.95, pot_odds + buffer)


def _recommend_facing_postflop_bet(
    profile: PostflopProfile,
    equity: float | None,
    pot_odds: float,
    players: int,
) -> tuple[str, str, float]:
    target = _range_adjusted_equity_target(pot_odds, players)
    has_target_equity = equity is not None and equity >= target

    if profile.plays_board:
        return "BOARD PLAYS. FOLD TO PRESSURE.", "The board supplies the made hand, so a bettor can still hold cards that improve on it.", target
    if profile.category in {"straight_flush", "four_kind", "full_house", "flush", "straight", "three_kind"}:
        if has_target_equity or profile.category in {"straight_flush", "four_kind", "full_house"}:
            return "STRONG MADE HAND. RAISE FOR VALUE.", "Your actual made hand supports continuing aggressively.", target
        return "STRONG MADE HAND. CALL, REASSESS LATER.", "The hand is strong, but the price and betting range argue against an automatic raise.", target
    if profile.category == "two_pair" and profile.pair_strength != "board_two_pair":
        if has_target_equity:
            return "TWO PAIR. CALL OR RAISE CAREFULLY.", "Two pair has real showdown strength, though coordinated boards can still contain stronger value.", target
        return "TWO PAIR UNDER PRESSURE. CALL CAUTIOUSLY.", "The made hand is real, but the price is demanding enough to avoid a large raise.", target
    if profile.category == "one_pair" and profile.pair_strength in {"top_pair", "overpair"}:
        if has_target_equity:
            return "ONE-PAIR HAND. CALL CAUTIOUSLY.", "Top pair or an overpair can continue at this price, but it is not automatically a raising hand.", target
        return "ONE PAIR, BAD PRICE. FOLD.", "A betting range is stronger than a random hand, and this price needs more than one-pair optimism.", target
    if profile.category == "one_pair" and profile.pair_strength in {"lower_pair", "underpair"}:
        if pot_odds <= 0.22 and has_target_equity:
            return "MARGINAL PAIR. CALL SMALL ONLY.", "The price is small enough for a cautious bluff catch, not a raise.", target
        return "MARGINAL PAIR. FOLD TO PRESSURE.", "A lower pair is too fragile against this price and a betting range.", target
    if profile.strong_draw:
        draw_price_limit = profile.next_card_draw_equity + (0.03 if players == 2 else 0.01)
        if pot_odds <= draw_price_limit:
            return "STRONG DRAW. CALL AT THIS PRICE.", "The immediate straight or flush draw has enough buffered equity to continue.", target
        return "DRAW, BUT PRICE IS TOO HIGH. FOLD.", "The direct draw odds plus a small implied-odds allowance do not cover this call price.", target
    if profile.gutshot:
        draw_price_limit = profile.next_card_draw_equity + (0.02 if players == 2 else 0.0)
        if pot_odds <= draw_price_limit:
            return "GUTSHOT. CALL SMALL ONLY.", "A very small price can justify chasing the four-out draw.", target
        return "GUTSHOT OR OVERCARDS. FOLD.", "Random-hand equity overstates this weak draw against a player who chose to bet.", target
    if profile.category == "one_pair" and profile.pair_strength == "board_pair":
        return "BOARD PAIR ONLY. FOLD.", "The pair belongs to the board; your hole cards have not made a pair.", target
    return "WEAK HAND VS BET. FOLD.", "High-card equity against random cards is not enough evidence to call an actual betting range.", target


def _recommend_when_checked_to(profile: PostflopProfile) -> tuple[str, str]:
    if profile.plays_board:
        return "BOARD PLAYS. CHECK.", "Your hole cards do not improve the board, so there is no clean value bet."
    if profile.category in {"straight_flush", "four_kind", "full_house", "flush", "straight", "three_kind"}:
        return "STRONG MADE HAND. BET FOR VALUE.", "This hand is strong enough to build the pot without relying on raw equity alone."
    if profile.category == "two_pair" and profile.pair_strength != "board_two_pair":
        return "TWO PAIR. BET SMALL TO MEDIUM.", "Charge one-pair hands while keeping the sizing controlled on coordinated boards."
    if profile.category == "one_pair" and profile.pair_strength in {"top_pair", "overpair"}:
        return "ONE-PAIR VALUE. BET SMALL TO MEDIUM.", "Top pair or an overpair can value bet, but it is not a monster by default."
    if profile.category == "one_pair" and profile.pair_strength in {"lower_pair", "underpair"}:
        return "MARGINAL PAIR. CHECK OR BET SMALL.", "Protect showdown value and avoid building a large pot with one fragile pair."
    if profile.strong_draw:
        return "STRONG DRAW. CHECK OR BET SMALL.", "A small semi-bluff is reasonable, but this is drawing equity rather than made-hand value."
    if profile.gutshot:
        return "WEAK DRAW. CHECK.", "Take the free card; a gutshot alone is not a value hand."
    if profile.category == "one_pair" and profile.pair_strength == "board_pair":
        return "BOARD PAIR ONLY. CHECK.", "Your hole cards have not made a pair, so avoid treating the board's pair as value."
    return "HIGH CARD. CHECK.", "Take the free option instead of converting random-hand equity into a value bet."


def _describe_current_hand_context(hero_cards: list[str], board_cards: list[str]) -> str:
    made_hand = describe_current_hand(hero_cards, board_cards)
    known_board = [card for card in board_cards if card]
    if len(known_board) < 3:
        return made_hand

    profile = analyze_postflop(hero_cards, known_board)
    if profile.pair_strength == "board_pair":
        return f"{made_hand} (pair is on the board)"
    if profile.pair_strength == "board_two_pair":
        return f"{made_hand} (both pairs are on the board)"
    if profile.plays_board:
        return f"{made_hand} (board plays)"
    return made_hand


SUPPORTED_BRIDGE_SITES = (
    BridgeSite(
        key="casino_org_replaypoker",
        label="casino.org/replaypoker",
        url="https://casino.org/replaypoker",
        tracker_site="ReplayPoker",
        allowed_origins=("https://casino.org", "https://www.casino.org"),
    ),
    BridgeSite(
        key="unibet_nl_pokerwebclient",
        label="unibet.nl/pokerwebclient",
        url="https://www.unibet.nl/play/pokerwebclient#playforreal",
        tracker_site="Unibet",
        allowed_origins=("https://www.unibet.nl", "https://unibet.nl"),
    ),
)
DEFAULT_BRIDGE_SITE = SUPPORTED_BRIDGE_SITES[0]
BRIDGE_SITES_BY_LABEL = {site.label: site for site in SUPPORTED_BRIDGE_SITES}
BRIDGE_ALLOWED_ORIGINS = {
    origin
    for site in SUPPORTED_BRIDGE_SITES
    for origin in site.allowed_origins
}
BRIDGE_USERSCRIPT_VERSION = "2.4"


class PreflopApp(tk.Tk):
    def __init__(self, use_custom_chrome: bool = True) -> None:
        super().__init__()
        self._use_custom_chrome = use_custom_chrome
        self._custom_chrome_backend = "override_redirect" if use_custom_chrome else "standard"
        self._startup_window_checked = False
        self._custom_chrome_ready = use_custom_chrome
        self._custom_chrome_startup_attempts = 0
        self.title("Poker Hand Trainers by Cha0i")
        self._set_initial_geometry()
        self.minsize(760, 520)
        self.resizable(True, True)
        self.configure(bg=BG_MAIN, highlightthickness=0, bd=0)
        self.option_add("*HighlightThickness", 0)
        if self._custom_chrome_backend == "override_redirect":
            self.overrideredirect(True)
        else:
            self.withdraw()
        self.fonts = {
            "title": tkfont.Font(family="Helvetica", size=BASE_FONT_SIZES["title"], weight="bold"),
            "subtitle": tkfont.Font(family="Helvetica", size=BASE_FONT_SIZES["subtitle"]),
            "section": tkfont.Font(family="Helvetica", size=BASE_FONT_SIZES["section"], weight="bold"),
            "card": tkfont.Font(family="Helvetica", size=BASE_FONT_SIZES["card"], weight="bold"),
            "label_large": tkfont.Font(family="Helvetica", size=BASE_FONT_SIZES["label_large"], weight="bold"),
            "label": tkfont.Font(family="Helvetica", size=BASE_FONT_SIZES["label"]),
            "small": tkfont.Font(family="Helvetica", size=BASE_FONT_SIZES["small"]),
            "window": tkfont.Font(family="Helvetica", size=BASE_FONT_SIZES["window"], weight="bold"),
        }

        self.selected: list[Card] = []
        self.card_buttons: dict[str, tk.Button] = {}
        self.hole_buttons: list[tk.Button] = []
        self.board_buttons: dict[str, tk.Button] = {}
        self.grid_frame: tk.Frame | None = None
        self.info_row: tk.Frame | None = None
        self.left_frame: tk.Frame | None = None
        self.right_frame: tk.Frame | None = None
        self.rank_frame: tk.Frame | None = None
        self.active_board_slot: str | None = None

        self.combo_var = tk.StringVar(value="Hand: -")
        self.score_var = tk.StringVar(value="Score: -")
        self.tier_var = tk.StringVar(value="Tier: -")
        self.advice_var = tk.StringVar(value="Advice: Select two hole cards")
        self.current_hand_var = tk.StringVar(value="Current hand: -")

        self.players_var = tk.IntVar(value=6)
        self.odds_status_var = tk.StringVar(value="Odds: select 2 hole cards")
        self.win_var = tk.StringVar(value="Win: -")
        self.tie_var = tk.StringVar(value="Tie: -")
        self.loss_var = tk.StringVar(value="Loss: -")
        self.equity_var = tk.StringVar(value="Total equity: -")
        self.odds_note_var = tk.StringVar(value="Select two hole cards to unlock board selection.")
        self.hand_rank_status_var = tk.StringVar(value="Hand odds: select 2 hole cards")
        self.hand_rank_you_vars = {key: tk.StringVar(value="-") for key, _label in HAND_CATEGORY_ORDER}
        self.hand_rank_other_vars = {key: tk.StringVar(value="-") for key, _label in HAND_CATEGORY_ORDER}

        self.score_label: tk.Label | None = None
        self.equity_label: tk.Label | None = None
        self.odds_note_label: tk.Label | None = None
        self.hand_rank_note_label: tk.Label | None = None
        self.site_selector_menu: tk.OptionMenu | None = None
        self.advice_label: tk.Label | None = None
        self.current_hand_label: tk.Label | None = None
        self.maximize_button: tk.Button | None = None
        self.hero_name_label: tk.Label | None = None
        self.hero_name_entry: tk.Entry | None = None
        self.hero_name_set_button: tk.Button | None = None
        self.clear_hole_button: tk.Button | None = None
        self.clear_board_button: tk.Button | None = None
        self.reset_all_button: tk.Button | None = None
        self.players_label: tk.Label | None = None
        self.players_value_label: tk.Label | None = None
        self.players_decrease_button: tk.Button | None = None
        self.players_increase_button: tk.Button | None = None
        self.server_frame: tk.Frame | None = None
        self.server_status_label: tk.Label | None = None
        self.server_info_label: tk.Label | None = None
        self.server_toggle_button: tk.Button | None = None
        self.server_log_widget: tk.Text | None = None
        self.strategy_frame: tk.Frame | None = None
        self.training_status_label: tk.Label | None = None
        self.strategy_context_label: tk.Label | None = None
        self.strategy_quick_label: tk.Label | None = None
        self.strategy_quick_sub_label: tk.Label | None = None
        self.strategy_advice_label: tk.Label | None = None
        self.player_tracker_frame: tk.Frame | None = None
        self.player_tracker_tab_button: tk.Button | None = None
        self.player_tracker_toggle_button: tk.Button | None = None
        self.player_tracker_total_label: tk.Label | None = None
        self.player_tracker_from_table_label: tk.Label | None = None
        self.player_tracker_table_label: tk.Label | None = None
        self.player_tracker_players_widget: tk.Text | None = None
        self.player_tracker_recent_cards: list[tk.Label] = []
        self.odds_after_id: str | None = None
        self.hand_rank_after_id: str | None = None
        self.odds_cache: dict[tuple[str, str, tuple[str, ...], int], tuple[float, float, float]] = {}
        self.hand_rank_cache: dict[tuple[str, str, tuple[str, ...]], tuple[dict[str, float], dict[str, float]]] = {}
        self._is_maximized = False
        self._restore_geometry = self.geometry()
        self._move_offset = (0, 0)
        self._resize_state: tuple[str, int, int, int, int, int, int] | None = None
        self._resize_job: str | None = None
        self._is_minimized = False

        self.server_status_var = tk.StringVar(value="Bridge: offline")
        self.site_var = tk.StringVar(value=DEFAULT_BRIDGE_SITE.label)
        self.server_info_var = tk.StringVar(value=self._bridge_idle_info())
        self.hero_name_input_var = tk.StringVar(value="xtlx")
        self.hero_name_var = tk.StringVar(value="player")
        self._hero_name_confirm_job: str | None = None
        self._server_running = False
        self._server: any = None
        self._server_thread: threading.Thread | None = None
        self._incoming_logs: Queue[str] = Queue()
        self._server_poll_job: str | None = None
        self._bridge_log_file = Path(__file__).resolve().parent / "browser-console.log"
        self._bridge_userscript_file = Path(__file__).resolve().parent / "tampermonkey-bridge.user.js"
        self._legacy_unibet_warning_sent = False
        self._app_actions_log_file = Path(__file__).resolve().parent / "app-actions.log"
        self._strategy_training_log_file = Path(__file__).resolve().parent / "strategy-training.log"
        self._player_tracker_db_file = Path(__file__).resolve().parent / "player-history.db"
        self._player_tracker_db: sqlite3.Connection | None = None
        self._tracker_visible = True
        self._tracker_column_width = 320
        self._tracker_tab_width = 40
        self._min_width_with_tracker = 980
        self._min_width_without_tracker = 760
        self._shell_frame: tk.Frame | None = None
        self._main_root_frame: tk.Frame | None = None
        self._current_table_id: str | None = None
        self._session_table_id: str | None = None
        self._current_session_id: str | None = None
        self._session_active = False
        self._session_table_delta = 0
        self._session_hands_buffer: list[dict[str, object]] = []
        self._active_hand_record: dict[str, object] | None = None
        self._player_screen_names: dict[int, str] = {}
        self._seat_user_map: dict[int, int] = {}
        self._current_hand_player_deltas: dict[int, int] = {}
        self._current_hand_player_contrib: dict[int, int] = {}
        self._current_hand_player_won: dict[int, int] = {}
        self._current_hand_player_stacks: dict[int, int] = {}
        self.strategy_context_var = tk.StringVar(value="Street: - | Pot: - | To call: - | Aggression: -")
        self.training_status_var = tk.StringVar(value="Training: paused (no hero hole cards)")
        self.strategy_quick_var = tk.StringVar(value="WAIT")
        self.strategy_quick_sub_var = tk.StringVar(value="No hole cards")
        self.player_tracker_total_var = tk.StringVar(value="♦ Total winnings: -")
        self.player_tracker_from_table_var = tk.StringVar(value="◇ From this table: -")
        self.player_tracker_table_var = tk.StringVar(value="⌂ Current table: -")
        self.strategy_advice_var = tk.StringVar(
            value=(
                "Smart play coach active. Start the bridge to read live bets and action flow. "
                "Baseline strategy is tight-aggressive with pot-odds discipline."
            )
        )
        self._street = "preflop"
        self._hero_seat_id: int | None = None
        self._hero_user_id: int | None = None
        self._hero_sitting_out: bool | None = None
        self._hero_turn: bool | None = None
        self._pot_chips: int | None = None
        self._to_call_chips: int | None = None
        self._minimum_raise_chips: int | None = None
        self._strategy_players_count: int | None = None
        self._allin_pressure = False
        self._hero_acted_preflop = False
        self._recent_actions: list[str] = []
        self._current_hand_id: int | None = None
        self._hand_last_advice_signature: str | None = None
        self._hand_outcome: str = "unknown"
        self._hand_hero_winnings: int | None = None
        self._hand_hero_showdown: dict[str, object] | None = None
        self._hero_hand_start_stack: int | None = None
        self._hero_hand_end_stack: int | None = None
        self._saw_showdown_this_hand = False
        self._awaiting_hero_hole_after_reset = False
        self._hero_folded_waiting_for_new_hole = False
        self._decision_advice_locks: dict[str, dict[str, str]] = {}
        self._seen_update_keys: set[tuple[int, int, str]] = set()
        self._seen_update_order: list[tuple[int, int, str]] = []
        self._seen_update_limit = 4000
        self._unibet_raw_hand_id: int | None = None
        self._unibet_raw_hole_key: str | None = None
        self._unibet_raw_hole_cards: list[str] | None = None
        self._unibet_raw_board_key: str | None = None
        self._unibet_raw_board_cards: list[str] = []
        self._unibet_raw_players_count: int | None = None
        self._unibet_raw_hero_sitting_out: bool | None = None
        self._unibet_raw_hero_folded: bool | None = None
        self._unibet_raw_hero_turn: bool | None = None
        self._unibet_raw_pot: int | None = None
        self._unibet_raw_to_call: int | None = None
        self._unibet_raw_minimum_raise: int | None = None
        self._unibet_raw_reset_sent = False
        self._bridge_card_table_id: str | None = None
        self._bridge_available = _BRIDGE_DEPENDENCIES_AVAILABLE
        self.site_var.trace_add("write", self._handle_site_changed)

        self.board_cards: dict[str, Card | None] = {
            "flop_1": None,
            "flop_2": None,
            "flop_3": None,
            "turn": None,
            "river": None,
        }

        if self._custom_chrome_backend == "override_redirect":
            self.bind("<Map>", self._restore_override_redirect)
        self.bind("<Configure>", self._schedule_layout_refresh)
        self._init_player_tracker_db()
        self._build_ui()
        self._refresh_buttons()
        self._refresh_hole_buttons()
        self._refresh_board_buttons()
        self._apply_scale()
        self._schedule_server_poll()
        if not self._bridge_available:
            self.server_status_var.set("Bridge: unavailable (install Flask and Werkzeug)")
            self.server_info_var.set("Core app will run without the browser bridge.")
        self.after_idle(self._present_startup_window)
        self.after(120, self._ensure_window_visible)
        self.after(900, self._recover_invisible_startup_window)

    def _set_initial_geometry(self) -> None:
        screen_width = max(900, self.winfo_screenwidth())
        screen_height = max(620, self.winfo_screenheight())
        width = max(760, min(900, screen_width - 80))
        height = max(520, min(1600, screen_height - 80))
        x = max(0, min(80, screen_width - width))
        y = max(0, min(80, screen_height - height))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def report_callback_exception(self, exc: type[BaseException], value: BaseException, tb: object) -> None:
        print("Poker Hand Trainer callback failed:", file=sys.stderr, flush=True)
        traceback.print_exception(exc, value, tb)

    def _clamp_window_to_screen(self) -> None:
        self.update_idletasks()
        width = max(self.winfo_width(), 760)
        height = max(self.winfo_height(), 520)
        screen_width = max(900, self.winfo_screenwidth())
        screen_height = max(620, self.winfo_screenheight())
        max_x = max(0, screen_width - min(width, screen_width))
        max_y = max(0, screen_height - min(height, screen_height))
        x = min(max(0, self.winfo_x()), max_x)
        y = min(max(0, self.winfo_y()), max_y)
        if (x, y) != (self.winfo_x(), self.winfo_y()):
            self.geometry(f"{width}x{height}+{x}+{y}")

    def _present_startup_window(self) -> None:
        if not self.winfo_exists():
            return
        try:
            self.update_idletasks()
            if self.state() in {"iconic", "withdrawn"}:
                self.deiconify()
            self.state("normal")
            self._clamp_window_to_screen()
            self.lift()
            self.attributes("-topmost", True)
            self.after(250, lambda: self.attributes("-topmost", False) if self.winfo_exists() else None)
            self.focus_force()
            self.update_idletasks()
        except tk.TclError:
            return

    def _activate_custom_chrome_after_startup(self) -> None:
        if self._custom_chrome_backend != "override_redirect" or not self.winfo_exists():
            return
        if self._custom_chrome_ready:
            return
        try:
            self._present_startup_window()
            if not (self.winfo_ismapped() and self.winfo_viewable()):
                self._custom_chrome_startup_attempts += 1
                if self._custom_chrome_startup_attempts <= 8:
                    self.after(250, self._activate_custom_chrome_after_startup)
                else:
                    print(
                        "Custom window chrome is waiting for the desktop to map the window.",
                        flush=True,
                    )
                return
            geometry = self.geometry()
            self.withdraw()
            self.update_idletasks()
            self.overrideredirect(True)
            self.geometry(geometry)
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()
            self.update_idletasks()
            if not (self.winfo_ismapped() and self.winfo_viewable()):
                self.overrideredirect(False)
                self.deiconify()
                self.state("normal")
                self._present_startup_window()
                print(
                    "Custom window chrome did not map cleanly; keeping the standard window visible.",
                    flush=True,
                )
                return
            self._custom_chrome_ready = True
        except tk.TclError:
            return

    def _recover_invisible_startup_window(self) -> None:
        if self._startup_window_checked or not self.winfo_exists():
            return
        self._startup_window_checked = True
        try:
            self._present_startup_window()
            if self.winfo_ismapped() and self.winfo_viewable():
                if self._custom_chrome_backend == "override_redirect" and not self._custom_chrome_ready:
                    self.after(100, self._activate_custom_chrome_after_startup)
                return
            if self._custom_chrome_backend == "override_redirect":
                print(
                    "Custom window chrome startup needed a remap; retrying borderless window presentation.",
                    flush=True,
                )
                self.geometry("+80+80")
                self.deiconify()
                self.state("normal")
                self._present_startup_window()
        except tk.TclError:
            return

    def _ensure_window_visible(self) -> None:
        if not self.winfo_exists():
            return
        try:
            self._clamp_window_to_screen()
            if self.state() == "iconic":
                self.deiconify()
            self.lift()
            self.focus_force()
        except tk.TclError:
            return

    def _format_card_display(self, card: Card) -> str:
        return f"{self._display_rank(card.rank)}{SUIT_SYMBOLS[card.suit]}"

    def _display_rank(self, rank: str) -> str:
        return "10" if rank == "T" else rank

    def _display_code(self, code: str) -> str:
        if len(code) != 2:
            return code.replace("T", "10")
        return f"{self._display_rank(code[0])}{code[1]}"

    def _display_hand_key(self, key: str) -> str:
        return key.replace("T", "10")

    def _suit_color(self, suit: str) -> str:
        return FG_RED if suit in {"h", "d"} else FG_LIGHT

    def _board_selection_background(self, slot: str | None) -> str:
        if slot == "turn":
            return BG_BUTTON_TURN_SELECTED
        if slot == "river":
            return BG_BUTTON_RIVER_SELECTED
        if slot is not None:
            return BG_BUTTON_SELECTED
        return BG_BUTTON

    def _score_color(self, score: int) -> str:
        if score < 25:
            return "#ff4d4d"
        if score < 50:
            return "#ff9f1c"
        if score < 75:
            return "#ffd60a"
        return "#2dc653"

    def _current_bridge_site(self) -> BridgeSite:
        try:
            label = self.site_var.get()
        except (AttributeError, tk.TclError):
            return DEFAULT_BRIDGE_SITE
        return BRIDGE_SITES_BY_LABEL.get(label, DEFAULT_BRIDGE_SITE)

    def _current_tracker_site(self) -> str:
        return self._current_bridge_site().tracker_site

    def _bridge_idle_info(self) -> str:
        site = self._current_bridge_site()
        return f"Selected site: {site.url}. Send tagged JSON: TM_BRIDGE:{{\"type\":\"poker_cards\",...}}"

    def _bridge_waiting_info(self) -> str:
        return f"Waiting for {self._current_bridge_site().label} tagged bridge lines..."

    def _handle_site_changed(self, *_args: object) -> None:
        if not getattr(self, "_bridge_available", True):
            return
        if getattr(self, "_server_running", False):
            self.server_info_var.set(self._bridge_waiting_info())
        else:
            self.server_info_var.set(self._bridge_idle_info())
        self._append_server_log(f"[bridge] selected site: {self._current_bridge_site().label}")

    def _build_ui(self) -> None:
        shell = tk.Frame(self, bg=BG_MAIN, highlightbackground="#27303d", highlightcolor="#27303d", highlightthickness=1, bd=0)
        shell.pack(fill="both", expand=True)
        self._shell_frame = shell
        shell.columnconfigure(0, weight=1)
        shell.columnconfigure(1, weight=0)
        shell.rowconfigure(1, weight=1)

        self._build_title_bar(shell)

        root = tk.Frame(shell, padx=10, pady=10, bg=BG_MAIN, highlightthickness=0, bd=0)
        root.grid(row=1, column=0, sticky="nsew")
        self._main_root_frame = root
        root.columnconfigure(0, weight=1, uniform="top")
        root.columnconfigure(1, weight=2, uniform="top")
        root.columnconfigure(2, weight=1, uniform="top")
        root.rowconfigure(1, weight=5)
        root.rowconfigure(2, weight=4)

        self._build_site_selector(root)

        top_center = tk.Frame(root, bg=BG_MAIN)
        top_center.grid(row=1, column=1, columnspan=2, sticky="nsew", padx=(8, 0))
        top_center.columnconfigure(0, weight=2)
        top_center.columnconfigure(1, weight=1)
        top_center.rowconfigure(0, weight=1)

        grid_frame = tk.Frame(top_center, bd=1, relief="solid", padx=6, pady=6, bg=BG_PANEL, highlightbackground="#27303d", highlightcolor="#27303d", highlightthickness=1)
        grid_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.grid_frame = grid_frame
        grid_frame.columnconfigure(0, weight=1)
        for col in range(1, 5):
            grid_frame.columnconfigure(col, weight=2, uniform="cards")
        for row in range(1, len(RANKS_DESC) + 1):
            grid_frame.rowconfigure(row, weight=1, uniform="cardrows")

        tk.Label(grid_frame, text="Rank", font=self.fonts["card"], bg=BG_PANEL, fg=FG_MAIN).grid(
            row=0, column=0, padx=3, pady=3, sticky="nsew"
        )
        for col, suit in enumerate(DISPLAY_SUITS, start=1):
            tk.Label(
                grid_frame,
                text=SUIT_SYMBOLS[suit],
                font=self.fonts["card"],
                bg=BG_PANEL,
                fg=self._suit_color(suit),
            ).grid(row=0, column=col, padx=3, pady=3, sticky="nsew")

        for row, rank in enumerate(RANKS_DESC, start=1):
            tk.Label(grid_frame, text=self._display_rank(rank), font=self.fonts["card"], bg=BG_PANEL, fg=FG_MAIN).grid(
                row=row, column=0, padx=3, pady=2, sticky="nsew"
            )
            for col, suit in enumerate(DISPLAY_SUITS, start=1):
                card = Card(rank=rank, suit=suit)
                button = tk.Button(
                    grid_frame,
                    text=self._format_card_display(card),
                    font=self.fonts["card"],
                    width=3,
                    height=2,
                    command=lambda c=card: self._handle_grid_card(c),
                    relief="flat",
                    bg=BG_BUTTON,
                    fg=self._suit_color(suit),
                    activebackground="#3a4556",
                    activeforeground=self._suit_color(suit),
                    highlightthickness=0,
                    bd=0,
                    cursor="hand2",
                )
                button.grid(row=row, column=col, padx=3, pady=2, sticky="nsew")
                self.card_buttons[card.code] = button

        self._build_server_column(root)
        self._build_strategy_column(top_center)

        info_row = tk.Frame(root, bg=BG_MAIN)
        info_row.grid(row=2, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        info_row.columnconfigure(0, weight=3)
        info_row.columnconfigure(1, weight=2)
        info_row.columnconfigure(2, weight=2)
        info_row.rowconfigure(0, weight=1)
        self.info_row = info_row

        self._build_left_column(info_row)
        self._build_right_column(info_row)
        self._build_rank_column(info_row)

        self._build_player_tracker_column(shell)
        shell.columnconfigure(1, minsize=self._tracker_column_width)
        self._set_player_tracker_visibility(False, resize_window=False)
        self._build_resize_grips(shell)

    def _build_site_selector(self, root: tk.Frame) -> None:
        frame = tk.Frame(root, bg=BG_MAIN)
        frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=0)
        frame.columnconfigure(2, weight=1)

        tk.Label(
            frame,
            text="Site:",
            font=self.fonts["label"],
            bg=BG_MAIN,
            fg=FG_MUTED,
        ).grid(row=0, column=0, sticky="w", padx=(0, 6))

        site_labels = [site.label for site in SUPPORTED_BRIDGE_SITES]
        self.site_selector_menu = tk.OptionMenu(frame, self.site_var, *site_labels)
        self.site_selector_menu.configure(
            font=self.fonts["label"],
            width=24,
            relief="flat",
            bg=BG_BUTTON,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.site_selector_menu.grid(row=0, column=1, sticky="w")
        menu = self.nametowidget(self.site_selector_menu["menu"])
        menu.configure(
            font=self.fonts["label"],
            bg=BG_PANEL,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            bd=0,
        )

    def _build_player_tracker_column(self, shell: tk.Frame) -> None:
        frame = tk.Frame(shell, bg=BG_PANEL, padx=10, pady=10, width=self._tracker_column_width, highlightbackground="#27303d", highlightcolor="#27303d", highlightthickness=1)
        frame.grid(row=1, column=1, sticky="nsew")
        frame.grid_propagate(False)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(6, weight=1)
        self.player_tracker_frame = frame

        tk.Label(frame, text="Player Tracker", font=self.fonts["section"], bg=BG_PANEL, fg=FG_MAIN).grid(row=0, column=0, sticky="w")
        self.player_tracker_total_label = tk.Label(
            frame,
            textvariable=self.player_tracker_total_var,
            font=self.fonts["label_large"],
            bg=BG_PANEL,
            fg="#2dc653",
            justify="left",
            anchor="w",
        )
        self.player_tracker_total_label.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.player_tracker_from_table_label = tk.Label(
            frame,
            textvariable=self.player_tracker_from_table_var,
            font=self.fonts["label"],
            bg=BG_PANEL,
            fg="#6fdc8c",
            justify="left",
            anchor="w",
        )
        self.player_tracker_from_table_label.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.player_tracker_table_label = tk.Label(
            frame,
            textvariable=self.player_tracker_table_var,
            font=self.fonts["label"],
            bg=BG_PANEL,
            fg=FG_MUTED,
            justify="left",
            anchor="w",
        )
        self.player_tracker_table_label.grid(row=3, column=0, sticky="ew", pady=(6, 0))

        tk.Label(frame, text="♣ Table stacks", font=self.fonts["small"], bg=BG_PANEL, fg=FG_MUTED).grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.player_tracker_players_widget = tk.Text(
            frame,
            width=28,
            height=8,
            wrap="word",
            bg="#11151c",
            fg=FG_MAIN,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#27303d",
            highlightcolor="#27303d",
            state="disabled",
        )
        self.player_tracker_players_widget.grid(row=5, column=0, sticky="ew", pady=(4, 0))

        tk.Label(frame, text="♠ Last 10 hands", font=self.fonts["small"], bg=BG_PANEL, fg=FG_MUTED).grid(row=6, column=0, sticky="w", pady=(10, 0))
        cards_box = tk.Frame(frame, bg=BG_PANEL)
        cards_box.grid(row=7, column=0, sticky="nsew", pady=(4, 0))
        cards_box.columnconfigure(0, weight=1)
        for row in range(10):
            card = tk.Label(
                cards_box,
                text=f"H{row + 1}: -",
                font=self.fonts["small"],
                bg=BG_BUTTON,
                fg=FG_MAIN,
                anchor="w",
                justify="left",
                padx=8,
                pady=6,
                highlightthickness=1,
                highlightbackground="#27303d",
                highlightcolor="#27303d",
            )
            card.grid(row=row, column=0, sticky="ew", pady=(0 if row == 0 else 4, 0))
            self.player_tracker_recent_cards.append(card)

        self.player_tracker_tab_button = tk.Button(
            shell,
            text="▶\nS\nT\nA\nT\nS",
            command=lambda: self._set_player_tracker_visibility(True),
            font=self.fonts["small"],
            relief="flat",
            bg=BG_BUTTON,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )

    def _resize_window_for_tracker_visibility(self, visible: bool) -> None:
        if self.state() != "normal":
            return
        if not self.winfo_exists():
            return

        delta = self._tracker_column_width - self._tracker_tab_width
        if delta <= 0:
            return

        current_width = self.winfo_width()
        current_height = self.winfo_height()
        current_x = self.winfo_x()
        current_y = self.winfo_y()

        if visible:
            target_width = current_width + delta
            min_width = self._min_width_with_tracker
        else:
            target_width = max(self._min_width_without_tracker, current_width - delta)
            min_width = self._min_width_without_tracker

        self.minsize(min_width, 520)
        self.geometry(f"{target_width}x{current_height}+{current_x}+{current_y}")

    def _set_player_tracker_visibility(self, visible: bool, resize_window: bool = True) -> None:
        if self._tracker_visible == visible:
            return
        self._tracker_visible = visible
        if self.player_tracker_frame is not None:
            if visible:
                self.player_tracker_frame.grid(row=1, column=1, sticky="nsew")
            else:
                self.player_tracker_frame.grid_remove()
        if self._shell_frame is not None:
            self._shell_frame.columnconfigure(1, minsize=(self._tracker_column_width if visible else self._tracker_tab_width))
        if self.player_tracker_tab_button is not None:
            if visible:
                self.player_tracker_tab_button.grid_remove()
            else:
                self.player_tracker_tab_button.grid(row=1, column=1, sticky="ns")
        if self.player_tracker_toggle_button is not None:
            self.player_tracker_toggle_button.configure(text=("Hide Stats" if visible else "Show Stats"))
        if resize_window:
            self._resize_window_for_tracker_visibility(visible)

    def _toggle_player_tracker_visibility(self) -> None:
        self._set_player_tracker_visibility(not self._tracker_visible)

    def _init_player_tracker_db(self) -> None:
        try:
            self._player_tracker_db = sqlite3.connect(self._player_tracker_db_file)
            cur = self._player_tracker_db.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS Players (
                    player_id INTEGER PRIMARY KEY,
                    screen_name TEXT,
                    site TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS Hands (
                    table_id TEXT NOT NULL,
                    hand_id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    blinds INTEGER,
                    button_seat INTEGER,
                    flop TEXT,
                    turn TEXT,
                    river TEXT,
                    final_pot INTEGER,
                    rake INTEGER
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS HandPlayers (
                    hand_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    seat INTEGER,
                    position TEXT,
                    starting_stack INTEGER,
                    ending_stack INTEGER,
                    hole_cards TEXT,
                    amount_contributed INTEGER,
                    amount_won INTEGER,
                    is_winner INTEGER,
                    PRIMARY KEY (hand_id, player_id),
                    FOREIGN KEY (hand_id) REFERENCES Hands(hand_id),
                    FOREIGN KEY (player_id) REFERENCES Players(player_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS Actions (
                    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hand_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    street TEXT NOT NULL,
                    action_order INTEGER NOT NULL,
                    action_type TEXT NOT NULL,
                    amount INTEGER,
                    FOREIGN KEY (hand_id) REFERENCES Hands(hand_id),
                    FOREIGN KEY (player_id) REFERENCES Players(player_id)
                )
                """
            )
            self._player_tracker_db.commit()
        except sqlite3.Error as error:
            self._player_tracker_db = None
            self._append_server_log(f"[tracker] sqlite init failed: {error}")

    def _new_session_id(self, table_id: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        hero = str(self._hero_user_id) if isinstance(self._hero_user_id, int) else "unknown"
        return f"{table_id}:{hero}:{stamp}"

    def _reset_session_tracking(self) -> None:
        self._session_active = False
        self._session_table_id = None
        self._current_session_id = None
        self._session_table_delta = 0
        self._session_hands_buffer = []
        self._active_hand_record = None
        self._current_hand_player_deltas = {}
        self._current_hand_player_contrib = {}
        self._current_hand_player_won = {}

    def _finalize_session_to_db(self) -> None:
        if self._player_tracker_db is None:
            self._reset_session_tracking()
            return
        if not self._session_hands_buffer:
            self._reset_session_tracking()
            return

        cur = self._player_tracker_db.cursor()
        try:
            for hand in self._session_hands_buffer:
                hand_id = hand.get("hand_id")
                if not isinstance(hand_id, int):
                    continue
                table_id = str(hand.get("table_id") or self._tracker_table_id())
                session_id = str(hand.get("session_id") or self._current_session_id or "session")
                cur.execute(
                    """
                    INSERT OR REPLACE INTO Hands
                    (hand_id, table_id, session_id, timestamp, blinds, button_seat, flop, turn, river, final_pot, rake)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hand_id,
                        table_id,
                        session_id,
                        str(hand.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
                        hand.get("blinds"),
                        hand.get("button_seat"),
                        hand.get("flop"),
                        hand.get("turn"),
                        hand.get("river"),
                        hand.get("final_pot"),
                        hand.get("rake"),
                    ),
                )

                players = hand.get("players")
                if isinstance(players, dict):
                    for player_id, pdata in players.items():
                        if not isinstance(player_id, int) or not isinstance(pdata, dict):
                            continue
                        screen_name = self._player_screen_names.get(player_id) or f"U{player_id}"
                        cur.execute(
                            """
                            INSERT OR REPLACE INTO Players (player_id, screen_name, site)
                            VALUES (?, ?, ?)
                            """,
                            (player_id, screen_name, self._current_tracker_site()),
                        )
                        cur.execute(
                            """
                            INSERT OR REPLACE INTO HandPlayers
                            (hand_id, player_id, seat, position, starting_stack, ending_stack, hole_cards, amount_contributed, amount_won, is_winner)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                hand_id,
                                player_id,
                                pdata.get("seat"),
                                pdata.get("position"),
                                pdata.get("starting_stack"),
                                pdata.get("ending_stack"),
                                pdata.get("hole_cards"),
                                pdata.get("amount_contributed", 0),
                                pdata.get("amount_won", 0),
                                1 if pdata.get("is_winner") else 0,
                            ),
                        )

                actions = hand.get("actions")
                if isinstance(actions, list):
                    for action in actions:
                        if not isinstance(action, dict):
                            continue
                        player_id = action.get("player_id")
                        if not isinstance(player_id, int):
                            continue
                        cur.execute(
                            """
                            INSERT INTO Actions (hand_id, player_id, street, action_order, action_type, amount)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                hand_id,
                                player_id,
                                str(action.get("street") or "Preflop"),
                                int(action.get("action_order") or 0),
                                str(action.get("action_type") or "Check"),
                                action.get("amount"),
                            ),
                        )
            self._player_tracker_db.commit()
        except sqlite3.Error as error:
            self._append_server_log(f"[tracker] session commit failed: {error}")
            self._player_tracker_db.rollback()
        self._reset_session_tracking()

    def _tracker_table_id(self) -> str:
        return self._current_table_id or "unknown"

    def _set_tracker_table_id(self, table_id: object) -> None:
        previous = self._current_table_id
        if isinstance(table_id, int):
            self._current_table_id = str(table_id)
        elif isinstance(table_id, str) and table_id.strip():
            self._current_table_id = table_id.strip()
        if previous and self._current_table_id and previous != self._current_table_id and self._session_active:
            self._finalize_session_to_db()

    def _update_seat_user_map(self, seats: object) -> None:
        if not isinstance(seats, list):
            return
        for seat in seats:
            if not isinstance(seat, dict):
                continue
            seat_id = seat.get("id")
            if not isinstance(seat_id, int):
                continue
            user_id = self._extract_player_user_id(seat)
            if user_id is not None:
                self._seat_user_map[seat_id] = user_id
                screen_name = seat.get("screenName") or seat.get("name") or seat.get("username")
                if isinstance(screen_name, str) and screen_name.strip():
                    self._player_screen_names[user_id] = screen_name.strip()
            if self._player_matches_hero_name(seat):
                allow_bind = False
                if self._hero_user_id is None and self._hero_seat_id is None:
                    allow_bind = True
                elif self._hero_user_id is not None and user_id == self._hero_user_id:
                    allow_bind = True
                elif self._hero_seat_id is not None and seat_id == self._hero_seat_id:
                    allow_bind = True
                if allow_bind:
                    self._hero_seat_id = seat_id
                    if user_id is not None:
                        self._hero_user_id = user_id
                    self._hero_sitting_out = self._player_is_sitting_out(seat)

    def _update_stack_snapshots(self, seats: object) -> None:
        if not isinstance(seats, list):
            return
        for seat in seats:
            if not isinstance(seat, dict):
                continue
            user_id = self._extract_player_user_id(seat)
            stack = seat.get("stack")
            if user_id is None or not isinstance(stack, int):
                continue
            self._current_hand_player_stacks[user_id] = stack
            if isinstance(self._active_hand_record, dict):
                players = self._active_hand_record.setdefault("players", {})
                pdata = players.setdefault(
                    user_id,
                    {
                        "seat": None,
                        "position": None,
                        "starting_stack": None,
                        "ending_stack": None,
                        "hole_cards": None,
                        "amount_contributed": 0,
                        "amount_won": 0,
                        "is_winner": False,
                    },
                )
                seat_id = seat.get("id")
                if isinstance(seat_id, int):
                    pdata["seat"] = seat_id
                if pdata.get("starting_stack") is None:
                    pdata["starting_stack"] = stack
                pdata["ending_stack"] = stack

    def _accumulate_awardpot_deltas(self, update: dict[str, object]) -> None:
        pot = update.get("pot")
        if not isinstance(pot, dict):
            return
        pot_players = pot.get("players")
        if not isinstance(pot_players, list):
            return
        for pot_player in pot_players:
            if not isinstance(pot_player, dict):
                continue
            seat_id = pot_player.get("seatId")
            if not isinstance(seat_id, int):
                continue
            user_id = self._seat_user_map.get(seat_id)
            if user_id is None:
                continue
            contribution = pot_player.get("contribution")
            winnings = pot_player.get("winnings")
            contribution_int = int(contribution) if isinstance(contribution, int) else 0
            winnings_int = int(winnings) if isinstance(winnings, int) else 0
            delta = winnings_int - contribution_int
            self._current_hand_player_deltas[user_id] = self._current_hand_player_deltas.get(user_id, 0) + delta
            self._current_hand_player_contrib[user_id] = self._current_hand_player_contrib.get(user_id, 0) + contribution_int
            self._current_hand_player_won[user_id] = self._current_hand_player_won.get(user_id, 0) + winnings_int

    def _ensure_active_hand(self, update: dict[str, object]) -> None:
        if not self._session_active:
            return
        hand_id_value = update.get("handId")
        if not isinstance(hand_id_value, int):
            return
        if isinstance(self._active_hand_record, dict) and self._active_hand_record.get("hand_id") == hand_id_value:
            return
        self._active_hand_record = {
            "hand_id": hand_id_value,
            "table_id": self._tracker_table_id(),
            "session_id": self._current_session_id or self._new_session_id(self._tracker_table_id()),
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "blinds": None,
            "button_seat": update.get("dealerSeat") if isinstance(update.get("dealerSeat"), int) else None,
            "flop": None,
            "turn": None,
            "river": None,
            "final_pot": None,
            "rake": None,
            "players": {},
            "actions": [],
            "action_order": 0,
        }
        self._current_hand_player_deltas = {}
        self._current_hand_player_contrib = {}
        self._current_hand_player_won = {}

    def _start_session_if_needed(self, seats: object) -> None:
        if self._hero_user_id is None:
            return
        if not isinstance(seats, list):
            return
        hero_seat = None
        for seat in seats:
            if not isinstance(seat, dict):
                continue
            if self._extract_player_user_id(seat) != self._hero_user_id:
                continue
            hero_seat = seat
            break
        if hero_seat is None:
            return
        if self._player_is_sitting_out(hero_seat):
            return

        table_id = self._tracker_table_id()
        if not self._session_active or self._session_table_id != table_id:
            if self._session_active:
                self._finalize_session_to_db()
            self._session_active = True
            self._session_table_id = table_id
            self._current_session_id = self._new_session_id(table_id)
            self._session_table_delta = 0
            self._session_hands_buffer = []
            self._active_hand_record = None

    def _record_action_event(self, player_id: int, action_type: str, amount: int | None = None, street: str | None = None) -> None:
        if not isinstance(self._active_hand_record, dict):
            return
        self._active_hand_record["action_order"] = int(self._active_hand_record.get("action_order", 0)) + 1
        entry = {
            "player_id": player_id,
            "street": street or self._street.title(),
            "action_order": self._active_hand_record["action_order"],
            "action_type": action_type,
            "amount": amount,
        }
        actions = self._active_hand_record.setdefault("actions", [])
        if isinstance(actions, list):
            actions.append(entry)

    def _action_type_label(self, action_lower: str) -> str:
        mapping = {
            "fold": "Fold",
            "check": "Check",
            "call": "Call",
            "bet": "Bet",
            "raise": "Raise",
            "allin": "All-In",
        }
        return mapping.get(action_lower, action_lower.title())

    def _resume_after_new_hole_if_ready(self) -> None:
        # Fold pause should only survive until a fresh 2-card hero hand is present.
        if self._hero_folded_waiting_for_new_hole and len(self.selected) == 2:
            self._hero_folded_waiting_for_new_hole = False

    def _record_blind_actions(self, update: dict[str, object]) -> None:
        players = update.get("players")
        if not isinstance(players, list):
            return
        for player in players:
            if not isinstance(player, dict):
                continue
            seat_id = player.get("seatId")
            if not isinstance(seat_id, int):
                continue
            player_id = self._seat_user_map.get(seat_id)
            if player_id is None:
                continue
            amount = player.get("bet")
            amount_int = int(amount) if isinstance(amount, int) else None
            self._record_action_event(player_id, "Post Blind", amount_int, "Preflop")

    def _hero_stood_up(self, update: dict[str, object]) -> bool:
        if not self._session_active or self._hero_user_id is None:
            return False

        seats = update.get("seats")
        if isinstance(seats, list):
            hero_found = False
            for seat in seats:
                if not isinstance(seat, dict):
                    continue
                if self._extract_player_user_id(seat) != self._hero_user_id:
                    continue
                hero_found = True
                state = str(seat.get("state", "")).lower()
                if state in {"available", "reserved"}:
                    return True
            if not hero_found:
                return True

        action = str(update.get("action", "")).lower()
        if action == "seat":
            seat = update.get("seat")
            if isinstance(seat, dict):
                state = str(seat.get("state", "")).lower()
                seat_user = self._extract_player_user_id(seat)
                if seat_user == self._hero_user_id and state in {"available", "reserved"}:
                    return True
        return False

    def _flush_hand_tracker(self) -> None:
        if not isinstance(self._active_hand_record, dict):
            return
        if self._current_hand_id is None:
            self._active_hand_record = None
            return
        record = self._active_hand_record
        record["final_pot"] = self._pot_chips if isinstance(self._pot_chips, int) else record.get("final_pot")

        players = record.setdefault("players", {})
        if isinstance(players, dict):
            for user_id, delta in self._current_hand_player_deltas.items():
                pdata = players.setdefault(
                    user_id,
                    {
                        "seat": None,
                        "position": None,
                        "starting_stack": None,
                        "ending_stack": self._current_hand_player_stacks.get(user_id),
                        "hole_cards": None,
                        "amount_contributed": 0,
                        "amount_won": 0,
                        "is_winner": False,
                    },
                )
                pdata["amount_contributed"] = self._current_hand_player_contrib.get(user_id, 0)
                pdata["amount_won"] = self._current_hand_player_won.get(user_id, 0)
                pdata["is_winner"] = self._current_hand_player_won.get(user_id, 0) > 0
                if pdata.get("ending_stack") is None:
                    pdata["ending_stack"] = self._current_hand_player_stacks.get(user_id)

        if isinstance(self._hero_user_id, int) and len(self.selected) == 2 and isinstance(players, dict):
            hero = players.setdefault(
                self._hero_user_id,
                {
                    "seat": self._hero_seat_id,
                    "position": None,
                    "starting_stack": self._hero_hand_start_stack,
                    "ending_stack": self._hero_hand_end_stack,
                    "hole_cards": None,
                    "amount_contributed": self._current_hand_player_contrib.get(self._hero_user_id, 0),
                    "amount_won": self._current_hand_player_won.get(self._hero_user_id, 0),
                    "is_winner": self._current_hand_player_won.get(self._hero_user_id, 0) > 0,
                },
            )
            hero["hole_cards"] = " ".join(card.code for card in self.selected)
            if hero.get("starting_stack") is None and isinstance(self._hero_hand_start_stack, int):
                hero["starting_stack"] = self._hero_hand_start_stack
            if isinstance(self._hero_hand_end_stack, int):
                hero["ending_stack"] = self._hero_hand_end_stack
            if self._hero_user_id not in self._current_hand_player_deltas and isinstance(self._hand_hero_winnings, int):
                if self._hand_hero_winnings >= 0:
                    hero["amount_won"] = self._hand_hero_winnings
                    hero["amount_contributed"] = 0
                    hero["is_winner"] = self._hand_hero_winnings > 0
                else:
                    hero["amount_won"] = 0
                    hero["amount_contributed"] = abs(self._hand_hero_winnings)
                    hero["is_winner"] = False

        self._session_hands_buffer.append(record)
        if isinstance(self._hero_user_id, int):
            hero_delta = self._current_hand_player_deltas.get(
                self._hero_user_id,
                self._hand_hero_winnings if isinstance(self._hand_hero_winnings, int) else 0,
            )
            self._session_table_delta += hero_delta
            record["hero_delta"] = hero_delta

        self._active_hand_record = None
        self._current_hand_player_deltas = {}
        self._current_hand_player_contrib = {}
        self._current_hand_player_won = {}

    def _refresh_player_tracker_panel(self) -> None:
        table_id = self._tracker_table_id()
        self.player_tracker_table_var.set(f"⌂ Current table: {table_id}")

        if self._player_tracker_db is None:
            self.player_tracker_total_var.set("♦ Total winnings: DB offline")
            self.player_tracker_from_table_var.set("◇ From this table: -")
            return

        hero_total = 0
        if isinstance(self._hero_user_id, int):
            row = self._player_tracker_db.execute(
                """
                SELECT COALESCE(SUM(COALESCE(amount_won,0) - COALESCE(amount_contributed,0)), 0)
                FROM HandPlayers
                WHERE player_id=?
                """,
                (self._hero_user_id,),
            ).fetchone()
            if row is not None:
                hero_total = int(row[0])

        sign = "+" if hero_total >= 0 else ""
        self.player_tracker_total_var.set(f"♦ Total winnings: {sign}{hero_total}")

        from_table_value = self._session_table_delta if self._session_active and self._session_table_id == table_id else 0
        from_table_sign = "+" if from_table_value >= 0 else ""
        self.player_tracker_from_table_var.set(f"◇ From this table: {from_table_sign}{from_table_value}")

        if self.player_tracker_players_widget is not None:
            lines = []
            for user_id, stack in sorted(self._current_hand_player_stacks.items(), key=lambda item: item[0]):
                lines.append(f"U{user_id}  stack {stack}")
            if not lines:
                lines = ["Waiting for table seat/stack data..."]
            self.player_tracker_players_widget.configure(state="normal")
            self.player_tracker_players_widget.delete("1.0", tk.END)
            self.player_tracker_players_widget.insert(tk.END, "\n".join(lines))
            self.player_tracker_players_widget.configure(state="disabled")

        recent: list[str] = []
        for hand in reversed(self._session_hands_buffer[-10:]):
            hand_id = hand.get("hand_id")
            hero_delta = hand.get("hero_delta", 0)
            if isinstance(hand_id, int):
                delta_int = int(hero_delta) if isinstance(hero_delta, int) else 0
                sign_delta = "+" if delta_int >= 0 else ""
                recent.append(f"H{hand_id}: Hero {sign_delta}{delta_int}")

        if len(recent) < 10:
            recent_rows = self._player_tracker_db.execute(
                """
                SELECT h.hand_id,
                       COALESCE(hp.amount_won, 0) - COALESCE(hp.amount_contributed, 0) AS hero_delta
                FROM Hands h
                LEFT JOIN HandPlayers hp
                  ON hp.hand_id = h.hand_id
                 AND hp.player_id = ?
                WHERE h.table_id = ?
                ORDER BY h.timestamp DESC
                LIMIT ?
                """,
                (self._hero_user_id or -1, table_id, max(0, 10 - len(recent))),
            ).fetchall()
            for hand_id, hero_delta in recent_rows:
                if isinstance(hand_id, int):
                    delta_int = int(hero_delta or 0)
                    sign_delta = "+" if delta_int >= 0 else ""
                    recent.append(f"H{hand_id}: Hero {sign_delta}{delta_int}")

        for idx, card in enumerate(self.player_tracker_recent_cards):
            if idx < len(recent):
                card.configure(text=f"▣ {recent[idx]}")
            else:
                card.configure(text=f"H{idx + 1}: -")

    def _build_server_column(self, root: tk.Frame) -> None:
        frame = tk.Frame(root, bg=BG_PANEL, padx=10, pady=10, highlightbackground="#27303d", highlightcolor="#27303d", highlightthickness=1)
        frame.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        frame.columnconfigure(0, weight=1)
        self.server_frame = frame

        tk.Label(frame, text="Browser Bridge", font=self.fonts["section"], bg=BG_PANEL, fg=FG_MAIN).grid(row=0, column=0, sticky="w")
        self.server_status_label = tk.Label(frame, textvariable=self.server_status_var, font=self.fonts["label"], bg=BG_PANEL, fg=FG_MAIN, anchor="w", justify="left", wraplength=280)
        self.server_status_label.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.server_info_label = tk.Label(frame, textvariable=self.server_info_var, font=self.fonts["small"], bg=BG_PANEL, fg=FG_MUTED, anchor="w", justify="left", wraplength=280)
        self.server_info_label.grid(row=2, column=0, sticky="ew", pady=(4, 0))

        buttons = tk.Frame(frame, bg=BG_PANEL)
        buttons.grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.server_toggle_button = tk.Button(
            buttons,
            text="Start Server",
            command=self._toggle_server,
            font=self.fonts["label"],
            width=12,
            relief="flat",
            bg=BG_BUTTON,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.server_toggle_button.grid(row=0, column=0, sticky="w")

        tk.Button(
            buttons,
            text="Clear Log",
            command=self._clear_server_log,
            font=self.fonts["label"],
            width=10,
            relief="flat",
            bg=BG_BUTTON,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        ).grid(row=1, column=0, pady=(6, 0), sticky="w")

        self.server_log_widget = tk.Text(
            frame,
            height=28,
            wrap="word",
            bg="#11151c",
            fg=FG_MAIN,
            insertbackground=FG_MAIN,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#27303d",
            highlightcolor="#27303d",
        )
        self.server_log_widget.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
        frame.rowconfigure(4, weight=1)

    def _build_strategy_column(self, root: tk.Frame) -> None:
        frame = tk.Frame(root, bg=BG_PANEL, padx=10, pady=10, highlightbackground="#27303d", highlightcolor="#27303d", highlightthickness=1)
        frame.grid(row=0, column=1, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(5, weight=1)
        self.strategy_frame = frame

        tk.Label(frame, text="Play Coach", font=self.fonts["section"], bg=BG_PANEL, fg=FG_MAIN).grid(row=0, column=0, sticky="w")
        self.training_status_label = tk.Label(
            frame,
            textvariable=self.training_status_var,
            font=self.fonts["small"],
            bg=BG_PANEL,
            fg=FG_MUTED,
            justify="left",
            anchor="w",
            wraplength=280,
        )
        self.training_status_label.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.strategy_context_label = tk.Label(
            frame,
            textvariable=self.strategy_context_var,
            font=self.fonts["small"],
            bg=BG_PANEL,
            fg=FG_MUTED,
            justify="left",
            anchor="w",
            wraplength=280,
        )
        self.strategy_context_label.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.strategy_quick_label = tk.Label(
            frame,
            textvariable=self.strategy_quick_var,
            font=self.fonts["label_large"],
            bg=BG_PANEL,
            fg=FG_MUTED,
            justify="left",
            anchor="w",
            wraplength=280,
        )
        self.strategy_quick_label.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.strategy_quick_sub_label = tk.Label(
            frame,
            textvariable=self.strategy_quick_sub_var,
            font=self.fonts["label_large"],
            bg=BG_PANEL,
            fg=FG_MUTED,
            justify="left",
            anchor="w",
            wraplength=280,
        )
        self.strategy_quick_sub_label.grid(row=4, column=0, sticky="ew", pady=(2, 0))
        self.strategy_advice_label = tk.Label(
            frame,
            textvariable=self.strategy_advice_var,
            font=self.fonts["label"],
            bg=BG_PANEL,
            fg=FG_MAIN,
            justify="left",
            anchor="nw",
            wraplength=280,
        )
        self.strategy_advice_label.grid(row=5, column=0, sticky="nsew", pady=(10, 0))

    def _strategy_quick_color(self, recommendation: str) -> str:
        mapping = {
            "fold": "#ff5f57",
            "check": "#f59e0b",
            "call": "#f59e0b",
            "call_or_raise": "#2dc653",
            "raise": "#2dc653",
            "bet": "#2dc653",
            "mixed": FG_MUTED,
            "wait": FG_MUTED,
        }
        return mapping.get(recommendation, FG_MUTED)

    def _strategy_quick_primary(self, recommendation: str) -> str:
        mapping = {
            "fold": "FOLD",
            "check": "CHECK",
            "call": "CALL",
            "call_or_raise": "CALL / RAISE",
            "raise": "RAISE",
            "bet": "BET",
            "mixed": "MIXED",
            "wait": "WAIT",
        }
        return mapping.get(recommendation, "PLAY")

    def _clear_server_log(self) -> None:
        if self.server_log_widget is not None:
            self.server_log_widget.delete("1.0", tk.END)

    def _append_server_log(self, line: str) -> None:
        if self.server_log_widget is None:
            return
        self.server_log_widget.insert(tk.END, f"{line}\n")
        self.server_log_widget.see(tk.END)

    def _log_app_action(self, event: str, **fields: object) -> None:
        noisy_events_without_hole = {
            "bridge_line_received",
            "process_console_line_start",
            "process_console_line_skip",
            "strategy_line_skip",
            "strategy_line_processed",
        }
        if len(self.selected) < 2 and event in noisy_events_without_hole:
            return
        if self._hero_sitting_out is True and event in noisy_events_without_hole:
            return
        if self._hero_folded_waiting_for_new_hole and event in noisy_events_without_hole:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            "selected": [card.code for card in self.selected],
            "board": {slot: (card.code if card is not None else None) for slot, card in self.board_cards.items()},
            "players": self.players_var.get(),
        }
        for key, value in fields.items():
            record[key] = value
        try:
            with self._app_actions_log_file.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(record, ensure_ascii=True))
                log_file.write("\n")
        except OSError:
            # Avoid impacting gameplay if file writes fail.
            pass

    def _strategy_headline(self) -> str:
        text = self.strategy_advice_var.get().strip()
        if not text:
            return ""
        return text.splitlines()[0].strip()

    def _strategy_recommendation(self) -> str:
        return self._recommendation_from_headline(self._strategy_headline())

    def _recommendation_from_headline(self, headline: str) -> str:
        headline = headline.upper()
        if "FOLD" in headline:
            return "fold"
        if "CALL" in headline and "RAISE" in headline:
            return "call_or_raise"
        if "CALL" in headline:
            return "call"
        if "BET" in headline:
            return "bet"
        if "RAISE" in headline or "RE-JAM" in headline or "JAM" in headline:
            return "raise"
        if "OPEN" in headline:
            return "raise"
        if "CHECK" in headline:
            return "check"
        if "WAIT" in headline:
            return "wait"
        return "mixed"

    def _decision_lock_key(self, players: int) -> str:
        payload = {
            "hand_id": self._current_hand_id,
            "street": self._street,
            "players": players,
            "pot": self._pot_chips,
            "to_call": self._to_call_chips,
            "hole": [card.code for card in self.selected],
            "board": [card.code for card in self.board_cards.values() if card is not None],
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=True)

    def _is_high_card_only(self, made_hand_text: str) -> bool:
        normalized = made_hand_text.strip().lower()
        if not normalized:
            return False
        return normalized.startswith("high card") or normalized.endswith(" high")

    def _enforce_advice_consistency(
        self,
        headline: str,
        details: list[str],
        made_hand_text: str,
        to_call: int,
    ) -> tuple[str, list[str]]:
        normalized_headline = headline.upper()
        high_card_only = self._is_high_card_only(made_hand_text)

        # Guard against contradictory "air" labels when we actually have made strength.
        if re.search(r"\bAIR\b", normalized_headline) and not high_card_only:
            if to_call > 0:
                headline = "SHOWDOWN VALUE. CALL SMALL, FOLD TO HEAT."
            else:
                headline = "SHOWDOWN VALUE. CHECK."
            if made_hand_text:
                details.insert(0, f"Made hand is {made_hand_text.lower()}, so this is not pure air.")

        # Guard against value headlines when the hand is really just high card.
        if "VALUE HAND" in headline.upper() and high_card_only:
            if to_call > 0:
                headline = "RIVER AIR. FOLD."
            else:
                headline = "RIVER AIR. CHECK."
            details.insert(0, "This is high-card showdown value only, not a clean value-bet spot.")

        return headline, details

    def _write_strategy_training_event(self, event: str, **fields: object) -> None:
        if self._hero_sitting_out is True:
            return
        if self._hero_folded_waiting_for_new_hole and event != "hole_set":
            return
        if len(self.selected) < 2 and event != "hole_set":
            return
        record: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "schema": "poker_strategy_training_v1",
            "event": event,
            "hand_id": self._current_hand_id,
            "street": self._street,
            "players": self.players_var.get(),
            "pot": self._pot_chips,
            "to_call": self._to_call_chips,
            "random_equity": self._equity_fraction(),
            "pot_odds": (
                self._to_call_chips / (self._pot_chips + self._to_call_chips)
                if isinstance(self._pot_chips, int)
                and isinstance(self._to_call_chips, int)
                and self._to_call_chips > 0
                and self._pot_chips + self._to_call_chips > 0
                else None
            ),
            "hole": [card.code for card in self.selected],
            "board": [card.code for card in self.board_cards.values() if card is not None],
            "advice_headline": self._strategy_headline(),
            "advice_recommendation": self._strategy_recommendation(),
        }
        board_codes = [card.code for card in self.board_cards.values() if card is not None]
        if len(self.selected) == 2 and len(board_codes) >= 3:
            profile = analyze_postflop([card.code for card in self.selected], board_codes)
            record.update(
                {
                    "made_hand": profile.made_hand,
                    "made_hand_category": profile.category,
                    "pair_strength": profile.pair_strength,
                    "draw_outs": profile.draw_outs,
                    "next_card_draw_equity": profile.next_card_draw_equity,
                    "range_equity_target": (
                        _range_adjusted_equity_target(record["pot_odds"], self.players_var.get())
                        if isinstance(record["pot_odds"], float)
                        else None
                    ),
                }
            )
        for key, value in fields.items():
            record[key] = value
        try:
            with self._strategy_training_log_file.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(record, ensure_ascii=True))
                log_file.write("\n")
        except OSError:
            # Do not break gameplay if logging fails.
            pass

    def _capture_advice_snapshot(self, trigger: str, force: bool = False) -> None:
        if self._hero_sitting_out is True:
            return
        if self._hero_folded_waiting_for_new_hole:
            return
        if len(self.selected) < 2:
            return
        signature_payload = {
            "hand_id": self._current_hand_id,
            "street": self._street,
            "players": self.players_var.get(),
            "pot": self._pot_chips,
            "to_call": self._to_call_chips,
            "hole": [card.code for card in self.selected],
            "board": [card.code for card in self.board_cards.values() if card is not None],
            "headline": self._strategy_headline(),
            "recommendation": self._strategy_recommendation(),
        }
        signature = json.dumps(signature_payload, sort_keys=True, ensure_ascii=True)
        if not force and signature == self._hand_last_advice_signature:
            return
        self._hand_last_advice_signature = signature
        self._write_strategy_training_event(
            "advice_snapshot",
            trigger=trigger,
            advice_text=self.strategy_advice_var.get(),
        )

    def _reset_hand_learning_state(self) -> None:
        self._hand_last_advice_signature = None
        self._hand_outcome = "unknown"
        self._hand_hero_winnings = None
        self._hand_hero_showdown = None
        self._hero_hand_start_stack = None
        self._hero_hand_end_stack = None
        self._saw_showdown_this_hand = False
        self._decision_advice_locks = {}

    def _set_server_status(self, text: str, online: bool) -> None:
        self.server_status_var.set(text)
        if self.server_status_label is not None:
            self.server_status_label.configure(fg="#2dc653" if online else FG_MAIN)

    def _toggle_server(self) -> None:
        if self._server_running:
            self._stop_server()
        else:
            self._start_server()

    def _start_server(self) -> None:
        if self._server_running:
            return
        if not self._bridge_available:
            self._set_server_status("Bridge: unavailable (install Flask and Werkzeug)", False)
            self.server_info_var.set("Core app will run without the browser bridge.")
            self._append_server_log("[bridge] startup skipped: Flask/Werkzeug not installed")
            return

        assert Flask is not None
        assert request is not None
        assert make_server is not None
        app = Flask("pokerodds_bridge")
        incoming = self._incoming_logs
        userscript_file = self._bridge_userscript_file

        def add_bridge_cors_headers(response: object) -> object:
            origin = request.headers.get("Origin")
            if origin in BRIDGE_ALLOWED_ORIGINS:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
                response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            return response

        app.after_request(add_bridge_cors_headers)

        @app.route("/log", methods=["OPTIONS"])
        def log_options() -> tuple[str, int]:
            return "", 204

        @app.post("/log")
        def receive_log() -> tuple[str, int]:
            line = redact_bridge_log_line((request.get_data(as_text=True) or "").strip())
            if line:
                is_legacy_unibet_discovery = (
                    line.startswith("[RAW_CONSOLE]")
                    and "[site:unibet_nl_pokerwebclient]" in line
                    and "[page:" not in line
                )
                if is_legacy_unibet_discovery:
                    if not self._legacy_unibet_warning_sent:
                        self._legacy_unibet_warning_sent = True
                        incoming.put(
                            "[bridge] ignored stale Unibet userscript output; install bridge userscript "
                            f"v{BRIDGE_USERSCRIPT_VERSION} from "
                            "http://127.0.0.1:5000/tampermonkey-bridge.user.js"
                        )
                    return "stale userscript ignored", 200
                incoming.put(line)
                try:
                    with self._bridge_log_file.open("a", encoding="utf-8") as logfile:
                        logfile.write(line)
                        logfile.write("\n")
                except OSError as error:
                    incoming.put(f"[bridge] failed writing browser-console.log: {error}")
            return "ok", 200

        @app.get("/health")
        def health() -> tuple[str, int]:
            return "ok", 200

        @app.get("/tampermonkey-bridge.user.js")
        def serve_userscript() -> object:
            try:
                source = userscript_file.read_text(encoding="utf-8")
            except OSError as error:
                return f"userscript unavailable: {error}", 500
            response = app.response_class(source, mimetype="application/javascript")
            response.headers["Cache-Control"] = "no-store"
            return response

        try:
            self._server = make_server("127.0.0.1", 5000, app)
        except OSError as error:
            self._set_server_status("Bridge: failed to bind 127.0.0.1:5000", False)
            self.server_info_var.set(f"Error: {error}")
            self._append_server_log(f"[bridge] startup failed: {error}")
            return

        self._legacy_unibet_warning_sent = False
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()
        self._server_running = True
        self._set_server_status("Bridge: online on 127.0.0.1:5000", True)
        self.server_info_var.set(self._bridge_waiting_info())
        if self.server_toggle_button is not None:
            self.server_toggle_button.configure(text="Stop Server")
        self._append_server_log("[bridge] server started on http://127.0.0.1:5000/log")
        self._append_server_log(
            "[bridge] install/update userscript: "
            "http://127.0.0.1:5000/tampermonkey-bridge.user.js"
        )

    def _stop_server(self) -> None:
        if not self._server_running:
            return
        try:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
        finally:
            self._server = None
            if self._server_thread is not None:
                self._server_thread.join(timeout=1.0)
            self._server_thread = None
            self._server_running = False
            self._set_server_status("Bridge: offline", False)
            self.server_info_var.set(self._bridge_idle_info())
            if self.server_toggle_button is not None:
                self.server_toggle_button.configure(text="Start Server")
            self._append_server_log("[bridge] server stopped")

    def _schedule_server_poll(self) -> None:
        self._server_poll_job = self.after(120, self._poll_server_queue)

    def _poll_server_queue(self) -> None:
        self._server_poll_job = None
        while True:
            try:
                line = self._incoming_logs.get_nowait()
            except Empty:
                break
            self._append_server_log(line)
            self._log_app_action("bridge_line_received", raw_line=line)
            self._process_console_line(line)
        if self.winfo_exists():
            self._schedule_server_poll()

    def _extract_cards_from_text(self, text: str) -> list[str]:
        return [match.upper() for match in re.findall(r"\b([2-9TJQKA][CDHScdhs])\b", text)]

    def _reset_unibet_raw_state(self, hand_id: int) -> None:
        self._unibet_raw_hand_id = hand_id
        self._unibet_raw_hole_key = None
        self._unibet_raw_hole_cards = None
        self._unibet_raw_board_key = None
        self._unibet_raw_board_cards = []
        self._unibet_raw_players_count = None
        self._unibet_raw_hero_sitting_out = None
        self._unibet_raw_hero_folded = None
        self._unibet_raw_hero_turn = None
        self._unibet_raw_pot = None
        self._unibet_raw_to_call = None
        self._unibet_raw_minimum_raise = None
        self._unibet_raw_reset_sent = False

    def _normalize_unibet_compact_cards(self, value: object, expected_count: int) -> list[str] | None:
        if not isinstance(value, str) or len(value) != expected_count * 2:
            return None
        cards: list[str] = []
        for index in range(0, len(value), 2):
            code = value[index : index + 2]
            if not re.match(r"^[2-9TJQKA][cdhs]$", code, re.IGNORECASE):
                return None
            cards.append(f"{code[0].upper()}{code[1].lower()}")
        return cards

    def _unibet_raw_active_players(self, states: object) -> int | None:
        if not isinstance(states, list):
            return None
        active = sum(1 for state in states if state == 1)
        if active <= 0:
            return None
        return max(2, min(10, active))

    def _unibet_raw_pot_size(self, compact_table: object) -> int | None:
        if not isinstance(compact_table, list):
            return None
        total = 0
        found = False
        bets = compact_table[3] if len(compact_table) > 3 and isinstance(compact_table[3], list) else []
        for bet in bets:
            if isinstance(bet, int) and bet >= 0:
                total += bet
                found = True
        pots = compact_table[4] if len(compact_table) > 4 and isinstance(compact_table[4], list) else []
        for pot in pots:
            if isinstance(pot, list) and pot and isinstance(pot[0], int) and pot[0] >= 0:
                total += pot[0]
                found = True
        return total if found else None

    def _unibet_raw_to_call_amount(self, compact_table: object, hero_seat_id: int | None) -> int | None:
        if not isinstance(compact_table, list) or hero_seat_id is None:
            return None
        bets = compact_table[3] if len(compact_table) > 3 and isinstance(compact_table[3], list) else None
        if bets is None or hero_seat_id < 0 or hero_seat_id >= len(bets) or not isinstance(bets[hero_seat_id], int):
            return None
        highest_bet = max((bet for bet in bets if isinstance(bet, int)), default=0)
        return max(0, highest_bet - bets[hero_seat_id])

    def _unibet_raw_minimum_raise_amount(self, compact_action: object) -> int | None:
        if not isinstance(compact_action, list) or len(compact_action) <= 3 or not isinstance(compact_action[3], list):
            return None
        for option in compact_action[3]:
            if isinstance(option, list) and len(option) > 1 and option[0] == 3 and isinstance(option[1], int):
                return max(0, option[1])
        return 0

    def _unibet_raw_player_names(self, compact_table: object) -> list[str] | None:
        if not isinstance(compact_table, list) or not compact_table or not isinstance(compact_table[0], str):
            return None
        return [name.strip().lower() for name in compact_table[0].split("|")]

    def _unibet_raw_hero_seat_from_names(self, compact_table: object) -> int | None:
        hero_name = self._hero_name()
        if not hero_name:
            return None
        names = self._unibet_raw_player_names(compact_table)
        if names is None:
            return None
        for seat_id, name in enumerate(names):
            if name == hero_name:
                return seat_id
        return None

    def _unibet_raw_accepts_player_context(
        self,
        compact_table: object,
        player_seat_id: int | None,
    ) -> bool:
        if player_seat_id is None:
            return False
        hero_seat_id = self._unibet_raw_hero_seat_from_names(compact_table)
        if hero_seat_id is not None:
            return player_seat_id == hero_seat_id
        names = self._unibet_raw_player_names(compact_table)
        if names is not None:
            return False
        return self._hero_seat_id is not None and player_seat_id == self._hero_seat_id

    def _extract_unibet_raw_relax_bodies(self, line: str) -> list[dict[str, object]]:
        if "[site:unibet_nl_pokerwebclient]" not in line or "<body" not in line:
            return []
        bodies: list[dict[str, object]] = []
        for match in re.finditer(r"<body\b[^>]*>([\s\S]*?)</body>", line, re.IGNORECASE):
            body_text = html.unescape(match.group(1))
            try:
                parsed = json.loads(body_text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                bodies.append(parsed)
        return bodies

    def _payload_from_unibet_raw_body(self, body: dict[str, object]) -> dict[str, object] | None:
        tags_raw = body.get("tags")
        compact_payload = body.get("payLoad")
        if not isinstance(tags_raw, list) or not isinstance(compact_payload, dict):
            return None
        tags = {str(tag) for tag in tags_raw}
        hand_id = compact_payload.get("hid")
        if not isinstance(hand_id, int):
            return None

        is_new_hand = self._unibet_raw_hand_id != hand_id
        if is_new_hand:
            self._reset_unibet_raw_state(hand_id)

        payload: dict[str, object] = {"type": "poker_cards", "handId": hand_id}
        table_id = compact_payload.get("tid")
        if isinstance(table_id, int):
            payload["tableId"] = table_id
        changed = False
        hole_changed = False
        if (is_new_hand or "init" in tags) and not self._unibet_raw_reset_sent:
            payload["reset"] = True
            payload["board"] = []
            self._unibet_raw_reset_sent = True
            self._unibet_raw_board_key = ""
            changed = True

        player_context = compact_payload.get("p") if isinstance(compact_payload.get("p"), list) else None
        compact_table = compact_payload.get("c") if isinstance(compact_payload.get("c"), list) else None
        player_seat_id = (
            player_context[1]
            if isinstance(player_context, list) and len(player_context) > 1 and isinstance(player_context[1], int)
            else None
        )
        named_hero_seat_id = self._unibet_raw_hero_seat_from_names(compact_table)
        hero_seat_id = named_hero_seat_id if named_hero_seat_id is not None else self._hero_seat_id
        if hero_seat_id is None and self._unibet_raw_accepts_player_context(compact_table, player_seat_id):
            hero_seat_id = player_seat_id
        if hero_seat_id is not None:
            payload["heroSeatId"] = hero_seat_id

        states = compact_table[1] if isinstance(compact_table, list) and len(compact_table) > 1 and isinstance(compact_table[1], list) else None
        players_count = self._unibet_raw_active_players(states)
        if players_count is not None and players_count != self._unibet_raw_players_count:
            self._unibet_raw_players_count = players_count
            payload["players"] = players_count
            changed = True

        if states is not None and hero_seat_id is not None and 0 <= hero_seat_id < len(states):
            hero_state = states[hero_seat_id]
            hero_sitting_out: bool | None = None
            if hero_state == 6:
                hero_sitting_out = True
            elif ("init" in tags or "deal" in tags) and hero_state == 1:
                hero_sitting_out = False
            if hero_sitting_out is not None and hero_sitting_out != self._unibet_raw_hero_sitting_out:
                self._unibet_raw_hero_sitting_out = hero_sitting_out
                payload["heroSittingOut"] = hero_sitting_out
                changed = True

            hero_folded = hero_state in {3, 4}
            if hero_folded != self._unibet_raw_hero_folded:
                self._unibet_raw_hero_folded = hero_folded
                payload["heroFolded"] = hero_folded
                changed = True

        compact_action = compact_payload.get("d") if isinstance(compact_payload.get("d"), list) else None
        hero_turn = self._unibet_raw_hero_turn
        if is_new_hand:
            hero_turn = False
        if "pturn" in tags and isinstance(compact_action, list) and compact_action and isinstance(compact_action[0], int):
            hero_turn = compact_action[0] == hero_seat_id
        elif (
            {"flop", "turn", "river", "finished"} & tags
            or ("act" in tags and isinstance(compact_action, list) and compact_action and compact_action[0] == hero_seat_id)
        ):
            hero_turn = False
        if hero_turn is not None and hero_turn != self._unibet_raw_hero_turn:
            self._unibet_raw_hero_turn = hero_turn
            payload["heroTurn"] = hero_turn
            changed = True

        pot = self._unibet_raw_pot_size(compact_table)
        if pot is not None and pot != self._unibet_raw_pot:
            self._unibet_raw_pot = pot
            payload["pot"] = pot
            changed = True

        to_call = self._unibet_raw_to_call_amount(compact_table, hero_seat_id)
        if to_call is not None and to_call != self._unibet_raw_to_call:
            self._unibet_raw_to_call = to_call
            payload["toCall"] = to_call
            changed = True

        if hero_turn is True and "pturn" in tags:
            minimum_raise = self._unibet_raw_minimum_raise_amount(compact_action)
            if minimum_raise is not None and minimum_raise != self._unibet_raw_minimum_raise:
                self._unibet_raw_minimum_raise = minimum_raise
                payload["minimumRaise"] = minimum_raise
                changed = True

        hole_cards = (
            self._normalize_unibet_compact_cards(player_context[3], 2)
            if isinstance(player_context, list) and len(player_context) > 3
            else None
        )
        is_reliable_hole_frame = bool({"deal", "pturn"} & tags)
        if hole_cards and is_reliable_hole_frame and self._unibet_raw_accepts_player_context(compact_table, player_seat_id):
            hole_key = "".join(hole_cards)
            if hole_key != self._unibet_raw_hole_key:
                self._unibet_raw_hole_key = hole_key
                hole_changed = True
                changed = True
            self._unibet_raw_hole_cards = hole_cards

        compact_board = compact_table[7] if isinstance(compact_table, list) and len(compact_table) > 7 else None
        board_cards = None
        if isinstance(compact_board, str) and len(compact_board) in {6, 8, 10}:
            board_cards = self._normalize_unibet_compact_cards(compact_board, len(compact_board) // 2)
        if board_cards:
            board_key = "".join(board_cards)
            if board_key != self._unibet_raw_board_key:
                self._unibet_raw_board_key = board_key
                changed = True
            self._unibet_raw_board_cards = board_cards

        if not changed:
            return None

        if self._unibet_raw_hole_cards and hole_changed:
            payload["hole"] = list(self._unibet_raw_hole_cards)
        if "board" not in payload:
            payload["board"] = list(self._unibet_raw_board_cards)
        if self._unibet_raw_hero_sitting_out is not None and "heroSittingOut" not in payload:
            payload["heroSittingOut"] = self._unibet_raw_hero_sitting_out
        if self._unibet_raw_hero_folded is not None and "heroFolded" not in payload:
            payload["heroFolded"] = self._unibet_raw_hero_folded
        if self._unibet_raw_hero_turn is not None and "heroTurn" not in payload:
            payload["heroTurn"] = self._unibet_raw_hero_turn
        return payload

    def _process_unibet_raw_relax_line(self, line: str) -> bool:
        applied = False
        for body in self._extract_unibet_raw_relax_bodies(line):
            payload = self._payload_from_unibet_raw_body(body)
            if payload is None:
                continue
            parsed = self._parse_tagged_bridge_payload(payload)
            if parsed is None:
                self._log_app_action("process_console_line_skip", reason="invalid_unibet_raw_payload", payload=payload)
                continue
            self._log_app_action("process_console_line_accept", source="unibet_raw_relax", payload=payload)
            (
                hole_cards,
                board_cards,
                players_count,
                reset_state,
                hand_id,
                table_id,
                hero_user_id,
                hero_seat_id,
                hero_sitting_out,
                hero_folded,
                pot_chips,
                to_call_chips,
                minimum_raise_chips,
                hero_turn,
            ) = parsed
            self._apply_external_cards(
                hole_cards,
                board_cards,
                players_count,
                reset_state,
                hand_id=hand_id,
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
            applied = True
        return applied

    def _parse_tagged_bridge_payload(
        self,
        payload: object,
    ) -> tuple[list[str], list[str] | None, int | None, bool, int | None, str | None, int | None, int | None, bool | None, bool | None, int | None, int | None, int | None, bool | None] | None:
        parsed = parse_bridge_payload(payload)
        if parsed is None:
            return None
        return (
            parsed.hole_cards,
            parsed.board_cards,
            parsed.players_count,
            parsed.reset_state,
            parsed.hand_id,
            parsed.table_id,
            parsed.hero_user_id,
            parsed.hero_seat_id,
            parsed.hero_sitting_out,
            parsed.hero_folded,
            parsed.pot_chips,
            parsed.to_call_chips,
            parsed.minimum_raise_chips,
            parsed.hero_turn,
        )

    def _extract_player_user_id(self, player: object) -> int | None:
        if not isinstance(player, dict):
            return None
        direct = player.get("userId")
        if isinstance(direct, int):
            return direct
        if isinstance(direct, str):
            trimmed = direct.strip()
            if trimmed.isdigit():
                return int(trimmed)
        nested = player.get("user")
        if isinstance(nested, dict):
            nested_id = nested.get("id")
            if isinstance(nested_id, int):
                return nested_id
            if isinstance(nested_id, str):
                trimmed_nested = nested_id.strip()
                if trimmed_nested.isdigit():
                    return int(trimmed_nested)
        return None

    def _hero_name(self) -> str:
        var = getattr(self, "hero_name_var", None)
        if var is None:
            return ""
        return var.get().strip().lower()

    def _set_hero_name(self) -> None:
        name = self.hero_name_input_var.get().strip()
        previous_name = self.hero_name_var.get().strip()
        self.hero_name_input_var.set(name)
        self.hero_name_var.set(name)
        if name != previous_name:
            self._hero_user_id = None
            self._hero_seat_id = None
            self._hero_sitting_out = None
        display_name = name or "(empty)"
        self._append_server_log(f"[bridge] player name set to {display_name!r}")
        self._log_app_action("hero_name_set", hero_name=name)
        self._flash_hero_name_set_button()
        self._update_strategy_panel()
        self._refresh_player_tracker_panel()

    def _flash_hero_name_set_button(self) -> None:
        if self.hero_name_set_button is None:
            return
        if self._hero_name_confirm_job is not None:
            try:
                self.after_cancel(self._hero_name_confirm_job)
            except tk.TclError:
                pass
        self.hero_name_set_button.configure(text="Set!")
        self._hero_name_confirm_job = self.after(1200, self._reset_hero_name_set_button)

    def _reset_hero_name_set_button(self) -> None:
        self._hero_name_confirm_job = None
        if self.hero_name_set_button is not None:
            self.hero_name_set_button.configure(text="Set")

    def _extract_player_name(self, player: object) -> str | None:
        if not isinstance(player, dict):
            return None
        for key in ("screenName", "name", "username"):
            value = player.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        user = player.get("user")
        if isinstance(user, dict):
            for key in ("screenName", "name", "username", "login"):
                value = user.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _player_matches_hero_name(self, player: object) -> bool:
        hero_name = self._hero_name()
        if not hero_name:
            return False
        player_name = self._extract_player_name(player)
        if not isinstance(player_name, str):
            return False
        return player_name.strip().lower() == hero_name

    def _player_is_sitting_out(self, player: object) -> bool:
        if not isinstance(player, dict):
            return False
        state = str(player.get("state", "")).lower()
        if state in {"sitout", "sittingout", "out"}:
            return True
        sitout_flags = ("sitOut", "sittingOut", "isSittingOut", "sitout")
        for flag in sitout_flags:
            value = player.get(flag)
            if value is True:
                return True
        return False

    def _process_console_line(self, raw_line: str) -> None:
        line = raw_line.strip()
        self._log_app_action("process_console_line_start", line=line)
        self._update_strategy_from_console_line(line)
        plain = re.sub(r"^\[[A-Z]+\]\s*", "", line)

        if not plain.startswith(BRIDGE_TAG):
            if self._process_unibet_raw_relax_line(line):
                return
            self._log_app_action("process_console_line_skip", reason="not_tagged")
            return

        payload_text = plain[len(BRIDGE_TAG):].strip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            self._append_server_log("[bridge] ignored tagged line: invalid JSON")
            self._log_app_action("process_console_line_skip", reason="invalid_json", payload_text=payload_text)
            return

        parsed = self._parse_tagged_bridge_payload(payload)
        if parsed is None:
            self._append_server_log("[bridge] ignored tagged line: invalid payload schema")
            self._log_app_action("process_console_line_skip", reason="invalid_payload_schema", payload=payload)
            return

        self._append_server_log(f"[bridge] accepted tagged payload: {payload!r}")
        self._log_app_action("process_console_line_accept", payload=payload)
        (
            hole_cards,
            board_cards,
            players_count,
            reset_state,
            hand_id,
            table_id,
            hero_user_id,
            hero_seat_id,
            hero_sitting_out,
            hero_folded,
            pot_chips,
            to_call_chips,
            minimum_raise_chips,
            hero_turn,
        ) = parsed
        self._apply_external_cards(
            hole_cards,
            board_cards,
            players_count,
            reset_state,
            hand_id=hand_id,
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

    def _extract_json_from_console_line(self, line: str) -> dict[str, object] | None:
        if "... [truncated]" in line or "...[truncated]" in line:
            return None
        marker_index = line.find("| {")
        if marker_index == -1:
            return None
        payload_text = line[marker_index + 2 :].strip()
        if not payload_text.startswith("{"):
            return None
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _street_label(self) -> str:
        mapping = {
            "preflop": "Preflop",
            "flop": "Flop",
            "turn": "Turn",
            "river": "River",
        }
        return mapping.get(self._street, self._street.title())

    def _aggression_label(self) -> str:
        recent = self._recent_actions[-8:]
        if not recent:
            return "Unknown"
        pressure = sum(1 for action in recent if action in {"bet", "raise", "allin"})
        if pressure >= 4:
            return "High"
        if pressure >= 2:
            return "Medium"
        return "Low"

    def _equity_fraction(self) -> float | None:
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)%", self.equity_var.get())
        if not match:
            return None
        return float(match.group(1)) / 100.0

    def _format_percent(self, value: float | None) -> str:
        if value is None:
            return "-"
        return f"{value * 100:.0f}%"

    def _players_active_from_snapshot(self, players: object) -> int | None:
        if not isinstance(players, list) or len(players) < 2:
            return None
        active = 0
        for player in players:
            if not isinstance(player, dict):
                continue
            state = str(player.get("state", "")).lower()
            if state in {"fold", "folded", "out", "sitout", "sittingout"}:
                continue
            active += 1
        if active <= 0:
            active = len(players)
        return max(2, min(10, active))

    def _is_duplicate_update(self, update: dict[str, object], action_lower: str) -> bool:
        sequence = update.get("sequence")
        if not isinstance(sequence, int):
            return False
        hand_id = update.get("handId")
        if not isinstance(hand_id, int):
            hand_id = self._current_hand_id if isinstance(self._current_hand_id, int) else -1
        key = (hand_id, sequence, action_lower)
        if key in self._seen_update_keys:
            return True
        self._seen_update_keys.add(key)
        self._seen_update_order.append(key)
        if len(self._seen_update_order) > self._seen_update_limit:
            old_key = self._seen_update_order.pop(0)
            self._seen_update_keys.discard(old_key)
        return False

    def _update_strategy_from_update(self, update: dict[str, object]) -> None:
        seats = update.get("seats")
        self._update_seat_user_map(seats)
        self._update_stack_snapshots(seats)

        hand_id_value = update.get("handId")
        if isinstance(hand_id_value, int):
            self._current_hand_id = hand_id_value

        action = str(update.get("action", "")).strip()
        if not action:
            return

        action_lower = action.lower()
        if self._is_duplicate_update(update, action_lower):
            self._log_app_action(
                "strategy_update_deduped",
                hand_id=update.get("handId"),
                sequence=update.get("sequence"),
                action=action_lower,
            )
            return
        self._recent_actions.append(action_lower)
        if len(self._recent_actions) > 24:
            self._recent_actions = self._recent_actions[-24:]

        self._ensure_active_hand(update)

        state_value = update.get("state")
        if isinstance(state_value, str):
            normalized = state_value.lower()
            if normalized in {"preflop", "flop", "turn", "river"}:
                self._street = normalized

        if action_lower == "starthand":
            self._reset_hand_learning_state()
            self._start_session_if_needed(update.get("seats"))
            self._current_hand_player_deltas = {}
            update_hand_id = update.get("id")
            if isinstance(update_hand_id, int):
                self._current_hand_id = update_hand_id
            self.selected = []
            for slot in BOARD_ORDER:
                self.board_cards[slot] = None
            self.active_board_slot = None
            self._awaiting_hero_hole_after_reset = True
            self._street = "preflop"
            self._pot_chips = None
            self._to_call_chips = None
            self._minimum_raise_chips = None
            self._recent_actions = []
            self._allin_pressure = False
            self._hero_acted_preflop = False
            self._hero_sitting_out = None
            # Keep strategy/training paused until the new hand's hero hole cards arrive.
            self._hero_folded_waiting_for_new_hole = True

            seats = update.get("seats")
            if isinstance(seats, list):
                for seat in seats:
                    if not isinstance(seat, dict):
                        continue
                    seat_user_id = self._extract_player_user_id(seat)
                    if self._hero_user_id is not None:
                        if seat_user_id != self._hero_user_id:
                            continue
                    elif not self._player_matches_hero_name(seat):
                        continue
                    seat_id = seat.get("id")
                    if isinstance(seat_id, int):
                        self._hero_seat_id = seat_id
                    if self._hero_user_id is None and seat_user_id is not None:
                        self._hero_user_id = seat_user_id
                    stack_value = seat.get("stack")
                    if isinstance(stack_value, int):
                        self._hero_hand_start_stack = stack_value
                    self._hero_sitting_out = self._player_is_sitting_out(seat)
                    break

                    self._ensure_active_hand(update)

        players = update.get("players")
        players_count = self._players_active_from_snapshot(players)
        if players_count is not None:
            self._strategy_players_count = players_count

        hero_player: dict[str, object] | None = None
        if isinstance(players, list):
            for player in players:
                if not isinstance(player, dict):
                    continue
                player_user_id = self._extract_player_user_id(player)
                matched = False
                if self._hero_user_id is not None and player_user_id == self._hero_user_id:
                    matched = True
                elif self._hero_user_id is None and self._player_matches_hero_name(player):
                    matched = True
                if matched:
                    hero_player = player
                    seat = player.get("seatId")
                    if isinstance(seat, int):
                        self._hero_seat_id = seat
                    if self._hero_user_id is None and player_user_id is not None:
                        self._hero_user_id = player_user_id
                    break

            if hero_player is None and self._hero_seat_id is not None:
                for player in players:
                    if not isinstance(player, dict):
                        continue
                    if player.get("seatId") == self._hero_seat_id:
                        hero_player = player
                        player_user_id = self._extract_player_user_id(player)
                        if player_user_id is not None:
                            self._hero_user_id = player_user_id
                        break

            if hero_player is not None:
                self._hero_sitting_out = self._player_is_sitting_out(hero_player)

        if action_lower == "starthand" and self._hero_sitting_out is not True:
            self._write_strategy_training_event(
                "hand_start",
                dealer_seat=update.get("dealerSeat"),
                seats=update.get("seats"),
            )

        if action_lower == "blinds":
            if isinstance(self._active_hand_record, dict):
                minimum_raise = update.get("minimumRaise")
                if isinstance(minimum_raise, int):
                    self._active_hand_record["blinds"] = minimum_raise
            self._record_blind_actions(update)

        seats_snapshot = update.get("seats")
        if isinstance(seats_snapshot, list):
            for seat in seats_snapshot:
                if not isinstance(seat, dict):
                    continue
                seat_user_id = self._extract_player_user_id(seat)
                if self._hero_user_id is not None:
                    if seat_user_id != self._hero_user_id:
                        continue
                elif not self._player_matches_hero_name(seat):
                    continue
                seat_id = seat.get("id")
                if isinstance(seat_id, int):
                    self._hero_seat_id = seat_id
                if self._hero_user_id is None and seat_user_id is not None:
                    self._hero_user_id = seat_user_id
                stack_value = seat.get("stack")
                if isinstance(stack_value, int) and self._hero_hand_start_stack is None:
                    self._hero_hand_start_stack = stack_value
                self._hero_sitting_out = self._player_is_sitting_out(seat)
                break

        if isinstance(players, list) and self._hero_seat_id is not None:
            hero = next((p for p in players if isinstance(p, dict) and p.get("seatId") == self._hero_seat_id), None)
            if isinstance(hero, dict):
                hero_bet = int(hero.get("bet", 0)) if isinstance(hero.get("bet", 0), int) else 0
                max_bet = 0
                for p in players:
                    if isinstance(p, dict) and isinstance(p.get("bet"), int):
                        max_bet = max(max_bet, int(p["bet"]))
                self._to_call_chips = max(0, max_bet - hero_bet)

        chips = update.get("chips")
        if isinstance(chips, int) and chips >= 0 and action_lower in {"bet", "raise", "allin", "call", "updatepots"}:
            if action_lower == "updatepots":
                self._pot_chips = chips
            else:
                self._pot_chips = (self._pot_chips or 0) + chips

        if action_lower == "allin":
            self._allin_pressure = True

        minimum_raise = update.get("minimumRaise")
        if isinstance(minimum_raise, int) and minimum_raise >= 0:
            self._minimum_raise_chips = minimum_raise
            if self._to_call_chips is None:
                self._to_call_chips = minimum_raise

        if action_lower == "awardpot":
            self._accumulate_awardpot_deltas(update)
            self._to_call_chips = 0
            self._allin_pressure = False
            players = update.get("players")
            if isinstance(players, list) and self._hero_seat_id is not None:
                for player in players:
                    if not isinstance(player, dict):
                        continue
                    if player.get("seatId") != self._hero_seat_id:
                        continue
                    winnings = player.get("winnings")
                    if isinstance(winnings, int):
                        self._hand_hero_winnings = winnings

            pot = update.get("pot")
            if isinstance(pot, dict) and self._hero_seat_id is not None:
                pot_players = pot.get("players")
                if isinstance(pot_players, list):
                    for player in pot_players:
                        if not isinstance(player, dict):
                            continue
                        if player.get("seatId") != self._hero_seat_id:
                            continue
                        winnings = player.get("winnings")
                        if isinstance(winnings, int):
                            self._hand_hero_winnings = winnings
                        break

            if isinstance(self._hand_hero_winnings, int):
                if self._hand_hero_winnings > 0:
                    self._hand_outcome = "won"
                elif self._hand_hero_winnings == 0 and self._saw_showdown_this_hand:
                    self._hand_outcome = "split"
                else:
                    self._hand_outcome = "lost"

        if action_lower == "showdown":
            self._saw_showdown_this_hand = True

        if action_lower == "show":
            shown_players = update.get("players")
            if isinstance(shown_players, list) and self._hero_seat_id is not None:
                for player in shown_players:
                    if not isinstance(player, dict):
                        continue
                    if player.get("seatId") != self._hero_seat_id:
                        continue
                    cards = player.get("cards")
                    hand_strength = player.get("handStrength")
                    self._hand_hero_showdown = {
                        "cards": cards,
                        "hand_strength": hand_strength,
                    }

        if action_lower == "dealcommunitycards":
            cards = update.get("cards")
            if isinstance(cards, list):
                self._write_strategy_training_event("community_reveal", cards=cards)
                if isinstance(self._active_hand_record, dict):
                    normalized = [str(card) for card in cards]
                    if len(normalized) == 3:
                        self._active_hand_record["flop"] = " ".join(normalized)
                    elif len(normalized) == 1:
                        if not self._active_hand_record.get("turn"):
                            self._active_hand_record["turn"] = normalized[0]
                        else:
                            self._active_hand_record["river"] = normalized[0]

        if action_lower in {"fold", "check", "call", "bet", "raise", "allin"}:
            seat_id = update.get("seatId")
            if isinstance(seat_id, int):
                actor_user_id = self._seat_user_map.get(seat_id)
                if actor_user_id is not None:
                    chips = update.get("chips")
                    amount = int(chips) if isinstance(chips, int) else None
                    self._record_action_event(actor_user_id, self._action_type_label(action_lower), amount, self._street.title())
            if self._hero_sitting_out is not True and self._hero_seat_id is not None and isinstance(seat_id, int) and seat_id == self._hero_seat_id:
                self._write_strategy_training_event(
                    "hero_action",
                    action=action_lower,
                    chips=update.get("chips"),
                    minimum_raise=update.get("minimumRaise"),
                )
                if self._street == "preflop":
                    self._hero_acted_preflop = True
                if action_lower == "fold":
                    self._hero_folded_waiting_for_new_hole = True

        if action_lower == "finishhand":
            seats = update.get("seats")
            if isinstance(seats, list) and self._hero_seat_id is not None:
                for seat in seats:
                    if not isinstance(seat, dict):
                        continue
                    if seat.get("id") != self._hero_seat_id:
                        continue
                    stack_value = seat.get("stack")
                    if isinstance(stack_value, int):
                        self._hero_hand_end_stack = stack_value
                    break

            if isinstance(self._hero_user_id, int):
                award = self._current_hand_player_won.get(self._hero_user_id)
                contributed = self._current_hand_player_contrib.get(self._hero_user_id)
                if isinstance(award, int) or isinstance(contributed, int):
                    award_int = int(award) if isinstance(award, int) else 0
                    contrib_int = int(contributed) if isinstance(contributed, int) else 0
                    self._hand_hero_winnings = award_int - contrib_int

            if self._hand_hero_winnings is None and isinstance(self._hero_hand_start_stack, int) and isinstance(self._hero_hand_end_stack, int):
                self._hand_hero_winnings = self._hero_hand_end_stack - self._hero_hand_start_stack

            if self._hand_outcome == "unknown" and isinstance(self._hand_hero_winnings, int):
                if self._hand_hero_winnings > 0:
                    self._hand_outcome = "won"
                elif self._hand_hero_winnings == 0 and self._saw_showdown_this_hand:
                    self._hand_outcome = "split"
                elif self._hand_hero_winnings < 0:
                    self._hand_outcome = "lost"

            if self._hero_sitting_out is not True:
                self._capture_advice_snapshot("finish_hand", force=True)
                self._write_strategy_training_event(
                    "hand_end",
                    outcome=self._hand_outcome,
                    hero_winnings=self._hand_hero_winnings,
                    hero_showdown=self._hand_hero_showdown,
                    hero_start_stack=self._hero_hand_start_stack,
                    hero_end_stack=self._hero_hand_end_stack,
                    finished_at=update.get("finishedAt"),
                )

            self._flush_hand_tracker()

            if self._hero_sitting_out is not True:
                if self._hero_stood_up(update):
                    self._finalize_session_to_db()

        self._refresh_player_tracker_panel()

    def _update_strategy_from_console_line(self, line: str) -> None:
        if not line.startswith("[RAW_CONSOLE]"):
            return

        auth_match = re.search(r"authenticated as userId\s+(\d+)", line)
        if auth_match:
            self._hero_user_id = int(auth_match.group(1))
            self._log_app_action("strategy_auth_detected", hero_user_id=self._hero_user_id)

        payload = self._extract_json_from_console_line(line)
        if payload is None:
            self._update_strategy_panel()
            self._log_app_action("strategy_line_skip", reason="no_json_payload")
            return

        payload_user = payload.get("userId")
        payload_table = payload.get("tableId")
        self._set_tracker_table_id(payload_table)
        if self._hero_user_id is None and isinstance(payload_table, int):
            if isinstance(payload_user, int):
                self._hero_user_id = payload_user
            elif isinstance(payload_user, str) and payload_user.strip().isdigit():
                self._hero_user_id = int(payload_user.strip())

        updates = payload.get("updates")
        if isinstance(updates, list):
            for update in updates:
                if isinstance(update, dict):
                    if "handId" not in update and isinstance(payload.get("handId"), int):
                        update["handId"] = payload.get("handId")
                    if "tableId" not in update and isinstance(payload.get("tableId"), int):
                        update["tableId"] = payload.get("tableId")
                    if "userId" not in update and isinstance(payload.get("userId"), int):
                        update["userId"] = payload.get("userId")
                    self._update_strategy_from_update(update)
        elif isinstance(payload.get("action"), str):
            self._update_strategy_from_update(payload)

        self._update_strategy_panel()
        self._refresh_player_tracker_panel()
        self._log_app_action("strategy_line_processed")

    def _update_strategy_panel(self) -> None:
        self._resume_after_new_hole_if_ready()

        street = self._street_label()
        pot_text = str(self._pot_chips) if isinstance(self._pot_chips, int) else "-"
        to_call_text = str(self._to_call_chips) if isinstance(self._to_call_chips, int) else "-"
        aggression = self._aggression_label()
        self.strategy_context_var.set(f"Street: {street} | Pot: {pot_text} | To call: {to_call_text} | Aggression: {aggression}")

        players_source = self._strategy_players_count if self._strategy_players_count is not None else self.players_var.get()
        players = max(2, min(10, players_source))

        if self._hero_sitting_out is True:
            self.training_status_var.set("Training: paused (hero sitting out)")
            self.strategy_quick_var.set("PAUSED")
            self.strategy_quick_sub_var.set("Sitting out")
            color = self._strategy_quick_color("wait")
            if self.strategy_quick_label is not None:
                self.strategy_quick_label.configure(fg=color)
            if self.strategy_quick_sub_label is not None:
                self.strategy_quick_sub_label.configure(fg=color)
            self.strategy_advice_var.set(
                "SITTING OUT. TRAINING PAUSED.\n"
                "No strategy or training events are logged until you are back in a hand."
            )
            return

        if self._hero_folded_waiting_for_new_hole:
            self.training_status_var.set("Training: paused (hero folded, waiting new hole cards)")
            self.strategy_quick_var.set("PAUSED")
            self.strategy_quick_sub_var.set("Hero folded")
            color = self._strategy_quick_color("wait")
            if self.strategy_quick_label is not None:
                self.strategy_quick_label.configure(fg=color)
            if self.strategy_quick_sub_label is not None:
                self.strategy_quick_sub_label.configure(fg=color)
            self.strategy_advice_var.set(
                "HERO FOLDED. TRACKING PAUSED.\n"
                "No table-action strategy logging until your next hole cards arrive."
            )
            return

        if len(self.selected) < 2:
            self.training_status_var.set("Training: paused (no hero hole cards)")
            self.strategy_quick_var.set("WAIT")
            self.strategy_quick_sub_var.set("No hole cards")
            color = self._strategy_quick_color("wait")
            if self.strategy_quick_label is not None:
                self.strategy_quick_label.configure(fg=color)
            if self.strategy_quick_sub_label is not None:
                self.strategy_quick_sub_label.configure(fg=color)
            self.strategy_advice_var.set(
                "WAIT FOR HOLE CARDS.\n"
                "No decision yet. Once your two cards arrive, the coach will give a direct action line first and short reasoning underneath."
            )
            self._capture_advice_snapshot("panel_wait")
            return

        if self._hero_turn is False:
            self.training_status_var.set("Training: waiting for hero turn")
            self.strategy_quick_var.set("WAIT")
            self.strategy_quick_sub_var.set("Another player is acting")
            color = self._strategy_quick_color("wait")
            if self.strategy_quick_label is not None:
                self.strategy_quick_label.configure(fg=color)
            if self.strategy_quick_sub_label is not None:
                self.strategy_quick_sub_label.configure(fg=color)
            self.strategy_advice_var.set(
                "WAIT FOR YOUR TURN.\n"
                "The table state is still updating; advice will refresh when action reaches you."
            )
            return

        self.training_status_var.set("Training: active")

        preflop = evaluate_preflop(self.selected[0].code, self.selected[1].code)
        equity = self._equity_fraction()
        to_call = self._to_call_chips if isinstance(self._to_call_chips, int) else 0
        pot = self._pot_chips if isinstance(self._pot_chips, int) else 0
        pot_odds = (to_call / (pot + to_call)) if (to_call > 0 and pot + to_call > 0) else None
        board_count = sum(1 for card in self.board_cards.values() if card is not None)
        board_codes = [card.code for card in self.board_cards.values() if card is not None]
        if board_count >= 3 and "..." in self.equity_var.get():
            self.training_status_var.set("Training: calculating equity")
            self.strategy_quick_var.set("WAIT")
            self.strategy_quick_sub_var.set("Calculating equity")
            color = self._strategy_quick_color("wait")
            if self.strategy_quick_label is not None:
                self.strategy_quick_label.configure(fg=color)
            if self.strategy_quick_sub_label is not None:
                self.strategy_quick_sub_label.configure(fg=color)
            self.strategy_advice_var.set(
                "CALCULATING POSTFLOP EQUITY.\n"
                "Advice will refresh as soon as the current board calculation finishes."
            )
            return
        made_hand = describe_current_hand([self.selected[0].code, self.selected[1].code], board_codes) if board_count >= 3 else None
        made_hand_lower = made_hand.lower() if isinstance(made_hand, str) else ""
        paired_board = len(board_codes) != len({code[0] for code in board_codes}) if board_codes else False
        suit_counts: dict[str, int] = {}
        for code in board_codes:
            suit_counts[code[1]] = suit_counts.get(code[1], 0) + 1
        max_suit_count = max(suit_counts.values()) if suit_counts else 0
        flush_heavy_board = max_suit_count >= 3
        preflop_allin_pressure = self._street == "preflop" and (
            self._allin_pressure
            or (to_call >= 12 and pot_odds is not None and pot_odds >= 0.40)
        )

        headline = "PLAY SOLID."
        details: list[str] = []
        quick_math_text: str | None = None

        if self._street == "preflop":
            if self._hero_acted_preflop and to_call == 0:
                headline = "PRE-FLOP DECISION MADE. WAIT FOR FLOP."
                details.append("You already acted preflop and there is no new price to call. Let the flop arrive before changing plans.")
            elif preflop_allin_pressure and to_call > 0:
                if preflop.tier == "Premium":
                    headline = "HEAVY PREFLOP PRESSURE. CALL OR RE-JAM."
                    details.append("A premium hand can continue, though stack depth and the opponent's range still affect the best line.")
                else:
                    headline = "HEAVY PREFLOP PRESSURE. FOLD WITHOUT A PREMIUM."
                    if equity is not None and pot_odds is not None:
                        details.append(
                            f"Random-hand equity about {self._format_percent(equity)} is not a valid stand-in for the range making this oversized raise."
                        )
                    details.append("Without effective stacks or a specific read, the conservative default is to fold non-premium hands to extreme pressure.")
            elif preflop.tier == "Premium":
                headline = "PREMIUM HAND. RAISE FOR VALUE."
                details.append("This belongs near the top of the preflop range. Raise for value rather than using postflop bet language.")
            elif preflop.tier == "Strong":
                if to_call > 0:
                    headline = "STRONG HAND. CALL OR 3-BET CAREFULLY."
                    details.append("This can continue against ordinary action, but facing a raise is not the same as opening an untouched pot.")
                else:
                    headline = "STRONG HAND. RAISE."
                    details.append("This is a clear value raise in an ordinary unopened pot, while position still matters.")
            elif preflop.tier == "Playable":
                if to_call > 0:
                    headline = "MARGINAL HAND. USUALLY FOLD."
                    details.append("This can open profitably in some positions, but it is not automatically strong enough to continue facing a raise.")
                else:
                    headline = "PLAYABLE HAND. OPEN SMALL."
                    details.append("Open small only when the action is unopened and your position permits it; this is not a premium hand.")
            elif preflop.tier == "Speculative":
                if to_call > 0:
                    headline = "SPECULATIVE HAND. FOLD TO PRESSURE."
                    details.append("Do not pay a raise with this hand without position, stack depth, and a specific exploit supporting the call.")
                else:
                    headline = "SPECULATIVE HAND. CHECK IF FREE, OTHERWISE FOLD."
                    details.append("Without reliable position information, the conservative default is to take a free option or fold.")
            else:
                headline = "TRASH HAND. FOLD."
                details.append("Without a real steal spot, this is not worth opening.")
        else:
            profile = analyze_postflop([self.selected[0].code, self.selected[1].code], board_codes)
            if to_call > 0 and pot_odds is not None:
                headline, reason, required_equity = _recommend_facing_postflop_bet(
                    profile,
                    equity,
                    pot_odds,
                    players,
                )
                details.append(reason)
                details.append(
                    f"Pot odds require {self._format_percent(pot_odds)}; random-hand equity is "
                    f"{self._format_percent(equity)}, and the conservative betting-range target is "
                    f"{self._format_percent(required_equity)}."
                )
                if profile.strong_draw or profile.gutshot:
                    quick_math_text = (
                        f"DRAW {self._format_percent(profile.next_card_draw_equity)} | "
                        f"PRICE {self._format_percent(pot_odds)}"
                    )
                    details.append(
                        f"The immediate draw has about {self._format_percent(profile.next_card_draw_equity)} "
                        "chance to improve on the next card before a small implied-odds allowance."
                    )
                else:
                    quick_math_text = (
                        f"PRICE {self._format_percent(pot_odds)} | "
                        f"RANGE {self._format_percent(required_equity)}"
                    )
            else:
                headline, reason = _recommend_when_checked_to(profile)
                details.append(reason)

            if pot_odds is not None and pot_odds >= 0.33:
                details.append("This is overbet-level call pressure, so the continuing range needs substantially more than raw pot odds.")
            elif pot_odds is not None and pot_odds >= 0.28:
                details.append("The large call price strengthens the range assumption and reduces marginal bluff catches.")

            if paired_board:
                details.append("Paired board: trips/full-house stories are over-represented in big bets, but weak players also stab these boards too wide.")
            if flush_heavy_board:
                details.append("Flush-heavy board: one-pair hands lose value fast, so avoid paying off because your hand looked pretty on the flop.")

        if players >= 6:
            details.append("Multiway table: people under-bluff and over-call, so value bet stronger and bluff less.")
        else:
            details.append("Short-handed table: ranges are wider and ego battles happen more, so thin value and bluff-catching improve.")

        if aggression == "High":
            details.append("Aggression is high, so do not level yourself into macho calls just because the line looks annoying.")
        elif aggression == "Low":
            details.append("Passive action usually means exactly what it looks like: weakness, capped ranges, and missed value bets.")

        if headline == "PLAY SOLID.":
            headline = "DEFAULT TO SMALL BALL."
            details.append("Nothing is screaming for a huge pot here. Keep ranges wide, sizes honest, and mistakes cheap.")

        headline, details = self._enforce_advice_consistency(
            headline,
            details,
            made_hand_lower,
            to_call,
        )

        advice_text = headline + "\n" + " ".join(details)
        recommendation = self._recommendation_from_headline(headline)
        decision_key = self._decision_lock_key(players)
        lock = self._decision_advice_locks.get(decision_key)
        if lock is not None and lock.get("recommendation") != recommendation:
            headline = lock.get("headline", headline)
            advice_text = lock.get("advice_text", advice_text)
            self._log_app_action(
                "strategy_decision_locked",
                decision_key=decision_key,
                locked_recommendation=lock.get("recommendation"),
                incoming_recommendation=recommendation,
            )
        else:
            self._decision_advice_locks[decision_key] = {
                "headline": headline,
                "recommendation": recommendation,
                "advice_text": advice_text,
            }

        quick_primary = self._strategy_quick_primary(recommendation)
        quick_secondary = (
            quick_math_text
            if quick_math_text is not None
            else f"RAND {self._format_percent(equity)} | PRICE {self._format_percent(pot_odds)}"
            if equity is not None and pot_odds is not None and to_call > 0
            else f"{street.upper()} | {players}P"
        )
        self.strategy_quick_var.set(quick_primary)
        self.strategy_quick_sub_var.set(quick_secondary)
        quick_color = self._strategy_quick_color(recommendation)
        if self.strategy_quick_label is not None:
            self.strategy_quick_label.configure(fg=quick_color)
        if self.strategy_quick_sub_label is not None:
            self.strategy_quick_sub_label.configure(fg=quick_color)

        self.strategy_advice_var.set(advice_text)
        self._capture_advice_snapshot("panel_update")

    def _card_from_code(self, code: str) -> Card | None:
        normalized = code.strip().upper()
        if len(normalized) != 2:
            return None
        rank = normalized[0]
        suit = normalized[1].lower()
        if rank not in RANKS_DESC or suit not in SUIT_SYMBOLS:
            return None
        return Card(rank=rank, suit=suit)

    def _apply_external_cards(
        self,
        hole_cards: list[str],
        board_cards: list[str] | None,
        players_count: int | None = None,
        reset_state: bool = False,
        hand_id: int | None = None,
        table_id: str | None = None,
        hero_user_id: int | None = None,
        hero_seat_id: int | None = None,
        hero_sitting_out: bool | None = None,
        hero_folded: bool | None = None,
        pot_chips: int | None = None,
        to_call_chips: int | None = None,
        minimum_raise_chips: int | None = None,
        hero_turn: bool | None = None,
    ) -> None:
        if hero_folded is True:
            self._hero_folded_waiting_for_new_hole = True
        elif hero_folded is False:
            self._hero_folded_waiting_for_new_hole = False

        accepting_fresh_hole = self._awaiting_hero_hole_after_reset

        self._log_app_action(
            "apply_external_cards_start",
            hole_cards=hole_cards,
            board_cards=board_cards,
            players_count=players_count,
            reset_state=reset_state,
            hand_id=hand_id,
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

        incoming_table_id = table_id.strip() if isinstance(table_id, str) and table_id.strip() else None
        locked_table_id = self.__dict__.get("_bridge_card_table_id")
        current_table_id = self.__dict__.get("_current_table_id")
        if incoming_table_id is not None and locked_table_id is not None and incoming_table_id != locked_table_id:
            self._append_server_log(
                f"[bridge] ignored payload for table {incoming_table_id}; locked to table {locked_table_id}"
            )
            self._log_app_action(
                "apply_external_cards_skip",
                reason="bridge_table_mismatch",
                table_id=incoming_table_id,
                locked_table_id=locked_table_id,
                hand_id=hand_id,
            )
            return
        if hand_id is not None:
            if self._current_hand_id is not None and hand_id < self._current_hand_id:
                self._append_server_log(f"[bridge] ignored stale payload for hand {hand_id} < current {self._current_hand_id}")
                self._log_app_action("apply_external_cards_skip", reason="stale_hand_id", hand_id=hand_id, current_hand_id=self._current_hand_id)
                return
            if self._current_hand_id is None or hand_id > self._current_hand_id:
                reset_state = True
            self._current_hand_id = hand_id

        if hero_user_id is not None:
            self._hero_user_id = hero_user_id
        if hero_seat_id is not None:
            self._hero_seat_id = hero_seat_id
        if hero_sitting_out is not None:
            self._hero_sitting_out = hero_sitting_out
        if hero_turn is not None:
            self._hero_turn = hero_turn

        parsed_hole: list[Card] = []
        parsed_board: list[Card] = []

        if reset_state:
            self.selected = []
            for slot in BOARD_ORDER:
                self.board_cards[slot] = None
            self._awaiting_hero_hole_after_reset = True
            self._street = "preflop"
            self._pot_chips = None
            self._to_call_chips = None
            self._minimum_raise_chips = None
            self._hero_turn = None
            self._strategy_players_count = None
            self._recent_actions = []

        if pot_chips is not None:
            self._pot_chips = pot_chips
        if to_call_chips is not None:
            self._to_call_chips = to_call_chips
        if minimum_raise_chips is not None:
            self._minimum_raise_chips = minimum_raise_chips
        if hero_turn is not None:
            self._hero_turn = hero_turn

        board_count = sum(1 for card in self.board_cards.values() if card is not None)

        if hole_cards:
            for code in hole_cards[:2]:
                card = self._card_from_code(code)
                if card is None:
                    self._append_server_log(f"[bridge] ignored invalid hole card: {code}")
                    self._log_app_action("apply_external_cards_skip", reason="invalid_hole_card", card=code)
                    return
                parsed_hole.append(card)
            if len(parsed_hole) != 2 or len({card.code for card in parsed_hole}) != 2:
                self._append_server_log("[bridge] ignored hole update with duplicate or incomplete cards")
                self._log_app_action("apply_external_cards_skip", reason="invalid_hole_set", parsed_hole=[card.code for card in parsed_hole])
                return
            if incoming_table_id is not None and self.__dict__.get("_bridge_card_table_id") is None:
                self._bridge_card_table_id = incoming_table_id
                self._set_tracker_table_id(incoming_table_id)
                self._log_app_action("bridge_table_locked", table_id=incoming_table_id, hand_id=hand_id)

            incoming_hole_codes = [card.code for card in parsed_hole]
            existing_hole_codes = [card.code for card in self.selected]

            # Keep hole cards immutable within a hand to prevent noisy payload overwrites.
            if (
                board_count > 0
                and len(existing_hole_codes) == 2
                and incoming_hole_codes != existing_hole_codes
                and not accepting_fresh_hole
            ):
                self._append_server_log("[bridge] ignored conflicting hole update after board started")
                self._log_app_action(
                    "apply_external_cards_skip",
                    reason="conflicting_hole_after_board_started",
                    incoming_hole=incoming_hole_codes,
                    existing_hole=existing_hole_codes,
                )
                parsed_hole = []
            elif len(existing_hole_codes) == 2 and incoming_hole_codes != existing_hole_codes and not accepting_fresh_hole:
                self._append_server_log("[bridge] ignored conflicting hole overwrite before reset")
                self._log_app_action(
                    "apply_external_cards_skip",
                    reason="hole_overwrite_before_reset",
                    incoming_hole=incoming_hole_codes,
                    existing_hole=existing_hole_codes,
                )
                parsed_hole = []

        if board_cards:
            for code in board_cards[:5]:
                card = self._card_from_code(code)
                if card is None:
                    self._append_server_log(f"[bridge] ignored invalid board card: {code}")
                    self._log_app_action("apply_external_cards_skip", reason="invalid_board_card", card=code)
                    return
                parsed_board.append(card)

        previous_hole_codes = [card.code for card in self.selected]
        if parsed_hole:
            self.selected = parsed_hole
            self._awaiting_hero_hole_after_reset = False
            self._hero_sitting_out = False
            self._resume_after_new_hole_if_ready()
        hole_changed = bool(parsed_hole) and [card.code for card in self.selected] != previous_hole_codes

        if board_cards is not None:
            if len(parsed_board) == 0:
                for slot in BOARD_ORDER:
                    self.board_cards[slot] = None

            existing_board_cards = [self.board_cards[slot] for slot in BOARD_ORDER if self.board_cards[slot] is not None]

            late_hole_received = bool(parsed_hole) and board_count > 0 and len(previous_hole_codes) == 0
            if hole_changed and not late_hole_received:
                for slot in BOARD_ORDER:
                    self.board_cards[slot] = None
                existing_board_cards = []

            if len(parsed_board) > 0 and len(parsed_board) >= len(existing_board_cards):
                for slot in BOARD_ORDER:
                    self.board_cards[slot] = None
                for slot, card in zip(BOARD_ORDER, parsed_board):
                    self.board_cards[slot] = card
            elif len(parsed_board) > 0:
                existing_codes = {card.code for card in existing_board_cards}
                new_cards = [card for card in parsed_board if card.code not in existing_codes]
                for card in new_cards:
                    next_slot = self._next_open_board_slot()
                    if next_slot is None:
                        break
                    self.board_cards[next_slot] = card

            applied_board_count = sum(1 for card in self.board_cards.values() if card is not None)
            if applied_board_count >= 5:
                self._street = "river"
            elif applied_board_count == 4:
                self._street = "turn"
            elif applied_board_count == 3:
                self._street = "flop"
            else:
                self._street = "preflop"

        if players_count is not None:
            self.players_var.set(players_count)
            self._strategy_players_count = players_count

        used_codes = {card.code for card in self.selected}
        board_used = {card.code for card in self.board_cards.values() if card is not None}
        if len(used_codes | board_used) != len(used_codes) + len(board_used):
            self._append_server_log("[bridge] ignored update with duplicate hole/board cards")
            self._log_app_action("apply_external_cards_skip", reason="duplicate_hole_board", used_codes=sorted(used_codes), board_used=sorted(board_used))
            return

        self.active_board_slot = self._next_open_board_slot() if len(self.selected) == 2 else None
        self._refresh_buttons()
        self._refresh_hole_buttons()
        self._refresh_board_buttons()
        self._update_outputs()

        hole_text = " ".join(card.code for card in self.selected) if self.selected else "(unchanged)"
        board_text = " ".join(card.code for card in self.board_cards.values() if card is not None)
        self.server_info_var.set(f"Applied hole [{hole_text}] board [{board_text or '-'}]")
        self._append_server_log(f"[bridge] applied hole [{hole_text}] board [{board_text or '-'}]")
        self._log_app_action("apply_external_cards_done", hole_text=hole_text, board_text=(board_text or "-"))
        if hole_changed:
            self._write_strategy_training_event("hole_set", source="bridge", hole=[card.code for card in self.selected])
        self._update_strategy_panel()

    def destroy(self) -> None:
        if self._hero_name_confirm_job is not None:
            try:
                self.after_cancel(self._hero_name_confirm_job)
            except tk.TclError:
                pass
            self._hero_name_confirm_job = None
        if self._server_poll_job is not None:
            try:
                self.after_cancel(self._server_poll_job)
            except tk.TclError:
                pass
            self._server_poll_job = None
        self._stop_server()
        if self._player_tracker_db is not None:
            try:
                self._player_tracker_db.close()
            except sqlite3.Error:
                pass
            self._player_tracker_db = None
        super().destroy()

    def _build_title_bar(self, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg=BG_PANEL, height=30, highlightthickness=0, bd=0)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)

        title = tk.Label(
            bar,
            text="  Poker Hand Trainer by Cha0i",
            font=self.fonts["window"],
            bg=BG_PANEL,
            fg=FG_MAIN,
            padx=8,
            pady=4,
        )
        title.grid(row=0, column=0, sticky="w")

        identity = tk.Frame(bar, bg=BG_PANEL)
        identity.grid(row=0, column=1, sticky="e", padx=(6, 10))
        self.hero_name_label = tk.Label(
            identity,
            text="Player:",
            font=self.fonts["small"],
            bg=BG_PANEL,
            fg=FG_MUTED,
        )
        self.hero_name_label.grid(row=0, column=0, padx=(0, 4), pady=1)
        self.hero_name_entry = tk.Entry(
            identity,
            textvariable=self.hero_name_input_var,
            width=12,
            font=self.fonts["small"],
            bg="#0f141b",
            fg=FG_MAIN,
            insertbackground=FG_MAIN,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#2b3442",
            highlightcolor="#4d6280",
        )
        self.hero_name_entry.grid(row=0, column=1, padx=(0, 0), pady=1)
        self.hero_name_entry.bind("<Return>", lambda _event: self._set_hero_name())
        self.hero_name_set_button = tk.Button(
            identity,
            text="Set",
            font=self.fonts["small"],
            width=4,
            command=self._set_hero_name,
            relief="flat",
            bg=BG_BUTTON,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.hero_name_set_button.grid(row=0, column=2, padx=(4, 0), pady=1)

        buttons = tk.Frame(bar, bg=BG_PANEL)
        buttons.grid(row=0, column=2, sticky="e")

        self.player_tracker_toggle_button = tk.Button(
            buttons,
            text="Hide Stats",
            font=self.fonts["small"],
            width=10,
            command=self._toggle_player_tracker_visibility,
            relief="flat",
            bg=BG_PANEL,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.player_tracker_toggle_button.grid(row=0, column=0, padx=(0, 4), pady=1)

        tk.Button(
            buttons,
            text="Min",
            font=self.fonts["small"],
            width=3,
            command=self._minimize_window,
            relief="flat",
            bg=BG_PANEL,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        ).grid(row=0, column=1, padx=(0, 1), pady=1)

        self.maximize_button = tk.Button(
            buttons,
            text="Max",
            font=self.fonts["small"],
            width=3,
            command=self._toggle_maximize,
            relief="flat",
            bg=BG_PANEL,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.maximize_button.grid(row=0, column=2, padx=1, pady=1)

        tk.Button(
            buttons,
            text="Close",
            font=self.fonts["small"],
            width=4,
            command=self.destroy,
            relief="flat",
            bg=BG_PANEL,
            fg=FG_MAIN,
            activebackground="#7f1d1d",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        ).grid(row=0, column=3, padx=(1, 4), pady=1)

        draggable = (bar, title)
        for widget in draggable:
            widget.bind("<ButtonPress-1>", self._start_window_move)
            widget.bind("<B1-Motion>", self._perform_window_move)
            widget.bind("<Double-Button-1>", lambda _event: self._toggle_maximize())

    def _build_resize_grips(self, parent: tk.Frame) -> None:
        if not self._use_custom_chrome:
            return

        grips = [
            ("n", "sb_v_double_arrow", {"relx": 0, "rely": 0, "relwidth": 1, "height": 6}),
            ("s", "sb_v_double_arrow", {"relx": 0, "rely": 1, "y": -6, "relwidth": 1, "height": 6}),
            ("w", "sb_h_double_arrow", {"x": 0, "rely": 0, "width": 10, "relheight": 1}),
            ("e", "sb_h_double_arrow", {"relx": 1, "x": -10, "rely": 0, "width": 10, "relheight": 1}),
            ("nw", "top_left_corner", {"x": 0, "y": 0, "width": 14, "height": 14}),
            ("ne", "top_right_corner", {"relx": 1, "x": -14, "y": 0, "width": 14, "height": 14}),
            ("sw", "bottom_left_corner", {"x": 0, "rely": 1, "y": -14, "width": 14, "height": 14}),
            ("se", "bottom_right_corner", {"relx": 1, "rely": 1, "x": -14, "y": -14, "width": 14, "height": 14}),
        ]
        for direction, cursor, place_args in grips:
            grip = tk.Frame(parent, bg=BG_MAIN, cursor=cursor, highlightthickness=0, bd=0)
            grip.place(**place_args)
            grip.bind("<ButtonPress-1>", lambda event, edge=direction: self._start_resize(event, edge))
            grip.bind("<B1-Motion>", self._perform_resize)
            grip.bind("<ButtonRelease-1>", self._finish_resize)

    def _restore_override_redirect(self, _event: tk.Event | None = None) -> None:
        if (
            self._custom_chrome_backend != "override_redirect"
            or not self._custom_chrome_ready
            or self.state() != "normal"
        ):
            return

        def restore_chrome() -> None:
            if not self.winfo_exists() or self.state() != "normal":
                return
            self.overrideredirect(True)
            self._is_minimized = False

        self.after(10, restore_chrome)

    def _start_window_move(self, event: tk.Event) -> None:
        if self._is_maximized:
            return
        self._move_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _perform_window_move(self, event: tk.Event) -> None:
        if self._is_maximized:
            return
        offset_x, offset_y = self._move_offset
        new_x = event.x_root - offset_x
        new_y = event.y_root - offset_y
        self.geometry(f"+{new_x}+{new_y}")

    def _minimize_window(self) -> None:
        if self._custom_chrome_backend == "override_redirect":
            self._is_minimized = True
            self.overrideredirect(False)
        self.iconify()

    def _finish_resize(self, _event: tk.Event | None = None) -> None:
        self._resize_state = None

    def _toggle_maximize(self) -> None:
        if self._is_maximized:
            self.geometry(self._restore_geometry)
            self._is_maximized = False
        else:
            self._restore_geometry = self.geometry()
            width = self.winfo_screenwidth()
            height = self.winfo_screenheight()
            self.geometry(f"{width}x{height}+0+0")
            self._is_maximized = True
        if self.maximize_button is not None:
            self.maximize_button.configure(text="Max" if not self._is_maximized else "Rest")

    def _start_resize(self, event: tk.Event, direction: str) -> None:
        if self._is_maximized:
            return
        self._resize_state = (
            direction,
            event.x_root,
            event.y_root,
            self.winfo_x(),
            self.winfo_y(),
            self.winfo_width(),
            self.winfo_height(),
        )

    def _perform_resize(self, event: tk.Event) -> None:
        if self._resize_state is None or self._is_maximized:
            return

        direction, start_x, start_y, win_x, win_y, width, height = self._resize_state
        delta_x = event.x_root - start_x
        delta_y = event.y_root - start_y
        min_width = 560
        min_height = 520

        new_x = win_x
        new_y = win_y
        new_width = width
        new_height = height

        if "e" in direction:
            new_width = max(min_width, width + delta_x)
        if "s" in direction:
            new_height = max(min_height, height + delta_y)
        if "w" in direction:
            new_width = max(min_width, width - delta_x)
            new_x = win_x + (width - new_width)
        if "n" in direction:
            new_height = max(min_height, height - delta_y)
            new_y = win_y + (height - new_height)

        self.geometry(f"{new_width}x{new_height}+{new_x}+{new_y}")

    def _build_left_column(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=BG_PANEL, padx=10, pady=10, highlightbackground="#27303d", highlightcolor="#27303d", highlightthickness=1)
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.left_frame = frame
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, weight=1)
        frame.columnconfigure(3, weight=1)

        tk.Label(frame, text="Hole Cards", font=self.fonts["section"], bg=BG_PANEL, fg=FG_MAIN).grid(row=0, column=0, columnspan=4, sticky="w")

        self.hole_buttons = []
        for idx in range(2):
            button = tk.Button(
                frame,
                text=f"Hole {idx + 1}",
                font=self.fonts["label"],
                width=6,
                height=2,
                relief="flat",
                bg=BG_BUTTON_LOCKED,
                fg=FG_MUTED,
                state="disabled",
                disabledforeground=FG_MUTED,
                highlightthickness=0,
                bd=0,
            )
            button.grid(row=1, column=idx, sticky="w", padx=(0 if idx == 0 else 3, 0), pady=(4, 0))
            self.hole_buttons.append(button)

        self.clear_hole_button = tk.Button(
            frame,
            text="Reset",
            command=self._clear_hole_cards,
            font=self.fonts["label"],
            width=8,
            height=2,
            relief="flat",
            bg=BG_BUTTON,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.clear_hole_button.grid(row=1, column=2, sticky="w", padx=(6, 0), pady=(4, 0))

        tk.Label(frame, textvariable=self.combo_var, font=self.fonts["label_large"], bg=BG_PANEL, fg=FG_MAIN).grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.score_label = tk.Label(frame, textvariable=self.score_var, font=self.fonts["label_large"], bg=BG_PANEL, fg=FG_MAIN)
        self.score_label.grid(row=3, column=0, columnspan=4, sticky="w", pady=(2, 0))
        tk.Label(frame, textvariable=self.tier_var, font=self.fonts["label"], bg=BG_PANEL, fg=FG_MAIN).grid(row=4, column=0, columnspan=4, sticky="w", pady=(2, 0))
        self.advice_label = tk.Label(
            frame,
            textvariable=self.advice_var,
            font=self.fonts["small"],
            justify="left",
            wraplength=360,
            bg=BG_PANEL,
            fg=FG_MUTED,
            anchor="w",
        )
        self.advice_label.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        self.current_hand_label = tk.Label(
            frame,
            textvariable=self.current_hand_var,
            font=self.fonts["label_large"],
            justify="left",
            wraplength=360,
            bg=BG_PANEL,
            fg=FG_MAIN,
            anchor="w",
        )
        self.current_hand_label.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(8, 0))

        self.players_label = tk.Label(frame, text="Players", font=self.fonts["label"], bg=BG_PANEL, fg=FG_MAIN)
        self.players_label.grid(row=7, column=0, sticky="w", pady=(8, 0))
        self.players_value_label = tk.Label(
            frame,
            textvariable=self.players_var,
            font=self.fonts["label_large"],
            bg=BG_BUTTON,
            fg=FG_MAIN,
            relief="flat",
            width=4,
            anchor="center",
            padx=8,
            pady=2,
        )
        self.players_value_label.grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.players_decrease_button = tk.Button(
            frame,
            text="-1P",
            command=lambda: self._change_players(-1),
            font=self.fonts["label"],
            width=6,
            height=2,
            relief="flat",
            bg=BG_BUTTON,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.players_decrease_button.grid(row=9, column=0, sticky="w", pady=(6, 0))
        self.players_increase_button = tk.Button(
            frame,
            text="+1P",
            command=lambda: self._change_players(1),
            font=self.fonts["label"],
            width=6,
            height=2,
            relief="flat",
            bg=BG_BUTTON,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.players_increase_button.grid(row=9, column=1, sticky="w", padx=(4, 0), pady=(6, 0))

    def _build_right_column(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=BG_PANEL, padx=10, pady=10, highlightbackground="#27303d", highlightcolor="#27303d", highlightthickness=1)
        frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self.right_frame = frame
        for col in range(4):
            frame.columnconfigure(col, weight=1)

        tk.Label(frame, text="Board and Odds", font=self.fonts["section"], bg=BG_PANEL, fg=FG_MAIN).grid(row=0, column=0, columnspan=4, sticky="w")

        slots = [
            ("flop_1", "Flop 1"),
            ("flop_2", "Flop 2"),
            ("flop_3", "Flop 3"),
            ("turn", "Turn"),
            ("river", "River"),
        ]
        for idx, (slot, label) in enumerate(slots):
            button = tk.Button(
                frame,
                text=label,
                font=self.fonts["label"],
                width=6,
                height=2,
                command=lambda s=slot: self._activate_board_slot(s),
                relief="flat",
                bg=BG_BUTTON,
                fg=FG_MAIN,
                activebackground="#3a4556",
                activeforeground=FG_MAIN,
                highlightthickness=0,
                bd=0,
                cursor="hand2",
            )
            button.grid(row=1 + idx // 3, column=idx % 3, padx=(0, 3), pady=(4, 0), sticky="w")
            self.board_buttons[slot] = button

        self.clear_board_button = tk.Button(
            frame,
            text="Reset",
            command=self._clear_board,
            font=self.fonts["label"],
            width=8,
            height=2,
            relief="flat",
            bg=BG_BUTTON,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.clear_board_button.grid(row=3, column=0, pady=(6, 0), sticky="w")

        self.reset_all_button = tk.Button(
            frame,
            text="Reset All",
            command=self._clear_all_cards,
            font=self.fonts["label"],
            width=8,
            height=2,
            relief="flat",
            bg=BG_BUTTON,
            fg=FG_MAIN,
            activebackground="#3a4556",
            activeforeground=FG_MAIN,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.reset_all_button.grid(row=10, column=0, columnspan=2, pady=(10, 0), sticky="w")

        tk.Label(frame, textvariable=self.odds_status_var, font=self.fonts["small"], bg=BG_PANEL, fg=FG_MUTED).grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        tk.Label(frame, textvariable=self.win_var, font=self.fonts["label"], bg=BG_PANEL, fg=FG_MAIN).grid(row=5, column=0, sticky="w", pady=(2, 0))
        tk.Label(frame, textvariable=self.tie_var, font=self.fonts["label"], bg=BG_PANEL, fg=FG_MAIN).grid(row=6, column=0, sticky="w", pady=(1, 0))
        tk.Label(frame, textvariable=self.loss_var, font=self.fonts["label"], bg=BG_PANEL, fg=FG_MAIN).grid(row=7, column=0, sticky="w", pady=(1, 0))

        self.equity_label = tk.Label(frame, textvariable=self.equity_var, font=self.fonts["label_large"], bg=BG_PANEL, fg=FG_MAIN)
        self.equity_label.grid(row=8, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self.odds_note_label = tk.Label(
            frame,
            textvariable=self.odds_note_var,
            font=self.fonts["small"],
            bg=BG_PANEL,
            fg=FG_MUTED,
            wraplength=300,
            justify="left",
        )
        self.odds_note_label.grid(row=9, column=0, columnspan=4, sticky="w", pady=(4, 0))

    def _build_rank_column(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=BG_PANEL, padx=10, pady=10, highlightbackground="#27303d", highlightcolor="#27303d", highlightthickness=1)
        frame.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        frame.columnconfigure(0, weight=3)
        frame.columnconfigure(1, weight=2)
        frame.columnconfigure(2, weight=2)
        self.rank_frame = frame

        tk.Label(frame, text="Hand Odds", font=self.fonts["section"], bg=BG_PANEL, fg=FG_MAIN).grid(row=0, column=0, columnspan=3, sticky="w")
        tk.Label(frame, text="Rank", font=self.fonts["small"], bg=BG_PANEL, fg=FG_MUTED).grid(row=1, column=0, sticky="w", pady=(6, 0))
        tk.Label(frame, text="You", font=self.fonts["small"], bg=BG_PANEL, fg=FG_MUTED).grid(row=1, column=1, sticky="e", pady=(6, 0))
        tk.Label(frame, text="Others", font=self.fonts["small"], bg=BG_PANEL, fg=FG_MUTED).grid(row=1, column=2, sticky="e", pady=(6, 0))

        for row_idx, (key, label) in enumerate(HAND_CATEGORY_ORDER, start=2):
            tk.Label(frame, text=label, font=self.fonts["small"], bg=BG_PANEL, fg=FG_MAIN, anchor="w").grid(
                row=row_idx, column=0, sticky="w", pady=(2, 0)
            )
            tk.Label(frame, textvariable=self.hand_rank_you_vars[key], font=self.fonts["small"], bg=BG_PANEL, fg=FG_MAIN, anchor="e").grid(
                row=row_idx, column=1, sticky="e", pady=(2, 0)
            )
            tk.Label(frame, textvariable=self.hand_rank_other_vars[key], font=self.fonts["small"], bg=BG_PANEL, fg=FG_MUTED, anchor="e").grid(
                row=row_idx, column=2, sticky="e", pady=(2, 0)
            )

        self.hand_rank_note_label = tk.Label(
            frame,
            textvariable=self.hand_rank_status_var,
            font=self.fonts["small"],
            bg=BG_PANEL,
            fg=FG_MUTED,
            wraplength=260,
            justify="left",
            anchor="w",
        )
        self.hand_rank_note_label.grid(row=len(HAND_CATEGORY_ORDER) + 2, column=0, columnspan=3, sticky="ew", pady=(8, 0))

    def _schedule_layout_refresh(self, event: tk.Event | None = None) -> None:
        if event is not None and event.widget is not self:
            return
        if not self.winfo_exists() or self._is_minimized:
            return
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except tk.TclError:
                pass
        self._resize_job = self.after(60, self._apply_scale)

    def _apply_scale(self) -> None:
        self._resize_job = None
        if not self.winfo_exists() or self._is_minimized or self.state() == "iconic":
            return
        width = max(self.winfo_width(), 560)
        content_width = width
        if self._main_root_frame is not None and self._main_root_frame.winfo_width() > 1:
            content_width = max(560, self._main_root_frame.winfo_width())
        height = max(self.winfo_height(), 520)
        scale = min(content_width / 920, height / 660)
        scale = max(0.9, min(1.35, scale))
        narrow = content_width < 760

        for name, base in BASE_FONT_SIZES.items():
            self.fonts[name].configure(size=max(8, round(base * scale)))

        card_width = 3 if scale < 1.05 else 4
        card_height = 2 if scale < 1.15 else 3
        for button in self.card_buttons.values():
            button.configure(width=card_width, height=card_height)
        for button in self.hole_buttons:
            button.configure(width=6 if scale < 1.1 else 7, height=card_height)
        for button in self.board_buttons.values():
            button.configure(width=6 if scale < 1.1 else 7, height=card_height)
        if self.clear_hole_button is not None:
            self.clear_hole_button.configure(width=8 if scale < 1.1 else 9, height=card_height)
        if self.clear_board_button is not None:
            self.clear_board_button.configure(width=8 if scale < 1.1 else 9, height=card_height)
        if self.reset_all_button is not None:
            self.reset_all_button.configure(width=8 if scale < 1.1 else 9, height=card_height)
        player_button_width = 6 if scale < 1.1 else 7
        if self.players_decrease_button is not None:
            self.players_decrease_button.configure(width=player_button_width, height=card_height)
        if self.players_increase_button is not None:
            self.players_increase_button.configure(width=player_button_width, height=card_height)
        if self.site_selector_menu is not None:
            self.site_selector_menu.configure(font=self.fonts["label"])
        panel_wrap = max(120, int((content_width - 84) / 3) - 20)
        text_wrap = max(120, min(250, panel_wrap))
        left_wrap = text_wrap
        right_wrap = text_wrap
        rank_wrap = text_wrap
        if self.left_frame is not None and self.left_frame.winfo_width() > 1:
            left_wrap = max(90, self.left_frame.winfo_width() - 28)
        if self.right_frame is not None and self.right_frame.winfo_width() > 1:
            right_wrap = max(90, self.right_frame.winfo_width() - 28)
        if self.rank_frame is not None and self.rank_frame.winfo_width() > 1:
            rank_wrap = max(90, self.rank_frame.winfo_width() - 28)
        strategy_wrap = text_wrap
        if self.strategy_frame is not None and self.strategy_frame.winfo_width() > 1:
            strategy_wrap = max(90, self.strategy_frame.winfo_width() - 28)
        if self.advice_label is not None:
            self.advice_label.configure(wraplength=left_wrap)
        if self.current_hand_label is not None:
            self.current_hand_label.configure(wraplength=left_wrap)
        if self.odds_note_label is not None:
            self.odds_note_label.configure(wraplength=right_wrap)
        if self.hand_rank_note_label is not None:
            self.hand_rank_note_label.configure(wraplength=rank_wrap)
        if self.server_status_label is not None and self.server_frame is not None and self.server_frame.winfo_width() > 1:
            self.server_status_label.configure(wraplength=max(120, self.server_frame.winfo_width() - 28))
        if self.server_info_label is not None and self.server_frame is not None and self.server_frame.winfo_width() > 1:
            self.server_info_label.configure(wraplength=max(120, self.server_frame.winfo_width() - 28))
        if self.strategy_context_label is not None:
            self.strategy_context_label.configure(wraplength=strategy_wrap)
        if self.strategy_advice_label is not None:
            self.strategy_advice_label.configure(wraplength=strategy_wrap)
        if self.player_tracker_frame is not None and self.player_tracker_frame.winfo_width() > 1:
            tracker_wrap = max(120, self.player_tracker_frame.winfo_width() - 28)
            if self.player_tracker_total_label is not None:
                self.player_tracker_total_label.configure(wraplength=tracker_wrap)
            if self.player_tracker_from_table_label is not None:
                self.player_tracker_from_table_label.configure(wraplength=tracker_wrap)
            if self.player_tracker_table_label is not None:
                self.player_tracker_table_label.configure(wraplength=tracker_wrap)
            for card in self.player_tracker_recent_cards:
                card.configure(wraplength=tracker_wrap)

        self._reflow_layout(narrow)

    def _reflow_layout(self, narrow: bool) -> None:
        if self.info_row is None or self.left_frame is None or self.right_frame is None or self.rank_frame is None:
            return

        self.info_row.columnconfigure(0, weight=3)
        self.info_row.columnconfigure(1, weight=2)
        self.info_row.columnconfigure(2, weight=2)
        self.info_row.rowconfigure(0, weight=1)
        self.info_row.rowconfigure(1, weight=0)
        self.left_frame.grid_configure(row=0, column=0, padx=(0, 4), pady=(0, 0), sticky="nsew")
        self.right_frame.grid_configure(row=0, column=1, padx=(4, 0), pady=(0, 0), sticky="nsew")
        self.rank_frame.grid_configure(row=0, column=2, padx=(4, 0), pady=(0, 0), sticky="nsew")

        if narrow:
            for idx, button in enumerate(self.hole_buttons):
                button.grid_configure(row=1 + idx, column=0, columnspan=3, padx=0, pady=(4 if idx == 0 else 2, 0), sticky="ew")
            if self.clear_hole_button is not None:
                self.clear_hole_button.grid_configure(row=3, column=0, columnspan=3, padx=0, pady=(6, 0), sticky="ew")

            for idx, slot in enumerate(BOARD_ORDER):
                self.board_buttons[slot].grid_configure(row=1 + idx, column=0, columnspan=3, padx=0, pady=(4 if idx == 0 else 2, 0), sticky="ew")
            if self.clear_board_button is not None:
                self.clear_board_button.grid_configure(row=6, column=0, columnspan=3, padx=0, pady=(6, 0), sticky="ew")
            if self.reset_all_button is not None:
                self.reset_all_button.grid_configure(row=10, column=0, columnspan=3, padx=0, pady=(10, 0), sticky="ew")
            if self.players_label is not None:
                self.players_label.grid_configure(row=7, column=0, columnspan=3, pady=(8, 0), sticky="w")
            if self.players_value_label is not None:
                self.players_value_label.grid_configure(row=8, column=0, columnspan=3, pady=(4, 0), sticky="w")
            if self.players_decrease_button is not None:
                self.players_decrease_button.grid_configure(row=9, column=0, columnspan=1, pady=(6, 0), sticky="ew")
            if self.players_increase_button is not None:
                self.players_increase_button.grid_configure(row=9, column=1, columnspan=1, padx=(4, 0), pady=(6, 0), sticky="ew")
        else:
            for idx, button in enumerate(self.hole_buttons):
                button.grid_configure(row=1, column=idx, columnspan=1, padx=(0 if idx == 0 else 3, 0), pady=(4, 0), sticky="w")
            if self.clear_hole_button is not None:
                self.clear_hole_button.grid_configure(row=1, column=2, columnspan=1, padx=(6, 0), pady=(4, 0), sticky="w")
            if self.players_label is not None:
                self.players_label.grid_configure(row=7, column=0, columnspan=2, pady=(8, 0), sticky="w")
            if self.players_value_label is not None:
                self.players_value_label.grid_configure(row=8, column=0, columnspan=2, pady=(4, 0), sticky="w")
            if self.players_decrease_button is not None:
                self.players_decrease_button.grid_configure(row=9, column=0, columnspan=1, pady=(6, 0), sticky="w")
            if self.players_increase_button is not None:
                self.players_increase_button.grid_configure(row=9, column=1, columnspan=1, padx=(4, 0), pady=(6, 0), sticky="w")

            for idx, slot in enumerate(BOARD_ORDER):
                self.board_buttons[slot].grid_configure(row=1 + idx // 3, column=idx % 3, columnspan=1, padx=(0, 3), pady=(4, 0), sticky="w")
            if self.clear_board_button is not None:
                self.clear_board_button.grid_configure(row=3, column=0, columnspan=1, padx=0, pady=(6, 0), sticky="w")
            if self.reset_all_button is not None:
                self.reset_all_button.grid_configure(row=10, column=0, columnspan=2, pady=(10, 0), sticky="w")

    def _board_used_codes(self, exclude_slot: str | None = None) -> set[str]:
        used: set[str] = set()
        for slot, card in self.board_cards.items():
            if slot == exclude_slot or card is None:
                continue
            used.add(card.code)
        return used

    def _handle_grid_card(self, card: Card) -> None:
        if len(self.selected) < 2:
            self._toggle_hole_card(card)
            return
        self._assign_board_card(card)

    def _toggle_hole_card(self, card: Card) -> None:
        if card.code in self._board_used_codes():
            self.advice_var.set("Advice: This card is already used on the board.")
            return

        existing = next((c for c in self.selected if c.code == card.code), None)
        if existing is not None:
            self.selected = [c for c in self.selected if c.code != card.code]
            self._refresh_buttons()
            self._refresh_hole_buttons()
            self._update_outputs()
            return

        if len(self.selected) >= 2:
            self.selected = [self.selected[1], card]
            self._refresh_buttons()
            self._refresh_hole_buttons()
            self._update_outputs(replaced=True)
            return

        self.selected.append(card)
        self._refresh_buttons()
        self._refresh_hole_buttons()
        self._resume_after_new_hole_if_ready()
        if len(self.selected) == 2 and self.active_board_slot is None:
            self.active_board_slot = self._next_open_board_slot()
        self._update_outputs()

    def _next_open_board_slot(self) -> str | None:
        for slot in BOARD_ORDER:
            if self.board_cards[slot] is None:
                return slot
        return None

    def _can_activate_board_slot(self, slot: str) -> bool:
        index = BOARD_ORDER.index(slot)
        return all(self.board_cards[previous] is not None for previous in BOARD_ORDER[:index])

    def _activate_board_slot(self, slot: str) -> None:
        if len(self.selected) < 2:
            self.odds_note_var.set("Select two hole cards before choosing flop, turn, or river.")
            return
        if not self._can_activate_board_slot(slot):
            self.odds_note_var.set("Select board cards in order: Flop 1, Flop 2, Flop 3, Turn, River.")
            return
        self.active_board_slot = slot
        self.odds_note_var.set(f"Choose {BOARD_LABELS[slot]} from the main card grid.")
        self._refresh_board_buttons()

    def _assign_board_card(self, card: Card) -> None:
        slot = self.active_board_slot or self._next_open_board_slot()
        if slot is None:
            self.odds_note_var.set("Board is full. Clear a board card or clear the board to continue.")
            return
        if not self._can_activate_board_slot(slot):
            self.odds_note_var.set("Select board cards in order: Flop 1, Flop 2, Flop 3, Turn, River.")
            return
        if card.code in {selected.code for selected in self.selected} or card.code in self._board_used_codes(exclude_slot=slot):
            self.odds_note_var.set("That card is already used by the hole cards or another board slot.")
            return

        self.board_cards[slot] = card
        self.active_board_slot = self._next_open_board_slot()
        if self.active_board_slot is None:
            self.odds_note_var.set("Board complete. Use the board buttons to replace a street if needed.")
        else:
            self.odds_note_var.set(f"Choose {BOARD_LABELS[self.active_board_slot]} from the main card grid.")
        self._refresh_board_buttons()
        self._refresh_buttons()
        self._update_outputs()

    def _refresh_buttons(self) -> None:
        selected_codes = {card.code for card in self.selected}
        board_codes = self._board_used_codes()

        for code, button in self.card_buttons.items():
            suit = code[1]
            text_color = self._suit_color(suit)
            if code in selected_codes:
                button.configure(relief="sunken", bg=BG_BUTTON_HOLE_SELECTED, fg=text_color)
            elif code in board_codes:
                button.configure(relief="flat", bg=BG_BUTTON_LOCKED, fg=text_color)
            elif len(self.selected) >= 2 and self.active_board_slot is not None:
                button.configure(relief="flat", bg="#2a3442", fg=text_color)
            else:
                button.configure(relief="flat", bg=BG_BUTTON, fg=text_color)

    def _refresh_hole_buttons(self) -> None:
        for idx, button in enumerate(self.hole_buttons):
            if idx < len(self.selected):
                card = self.selected[idx]
                button.configure(
                    text=f"{self._format_card_display(card)} ({self._display_code(card.code)})",
                    font=self.fonts["label"],
                    bg=BG_BUTTON_LOCKED,
                    fg=self._suit_color(card.suit),
                    disabledforeground=self._suit_color(card.suit),
                )
            else:
                button.configure(text=f"Hole {idx + 1}", bg=BG_BUTTON_LOCKED, fg=FG_MUTED, disabledforeground=FG_MUTED)

    def _refresh_board_buttons(self) -> None:
        for slot, button in self.board_buttons.items():
            card = self.board_cards[slot]
            background = self._board_selection_background(slot) if slot == self.active_board_slot else BG_BUTTON
            if card is None:
                button.configure(text=BOARD_LABELS[slot], fg=FG_MAIN, bg=background)
            else:
                button.configure(text=f"{BOARD_LABELS[slot]}: {self._format_card_display(card)}", fg=self._suit_color(card.suit), bg=background)

    def _clear_hole_cards(self) -> None:
        self.selected = []
        self.active_board_slot = None
        self._refresh_buttons()
        self._refresh_hole_buttons()
        self._refresh_board_buttons()
        self._update_outputs()

    def _clear_board(self) -> None:
        for slot in self.board_cards:
            self.board_cards[slot] = None
        self.active_board_slot = self._next_open_board_slot() if len(self.selected) == 2 else None
        if len(self.selected) == 2 and self.active_board_slot is not None:
            self.odds_note_var.set(f"Choose {BOARD_LABELS[self.active_board_slot]} from the main card grid.")
        else:
            self.odds_note_var.set("Select two hole cards to unlock board selection.")
        self._refresh_board_buttons()
        self._refresh_buttons()
        self._update_outputs()

    def _clear_all_cards(self) -> None:
        self.selected = []
        for slot in self.board_cards:
            self.board_cards[slot] = None
        self.active_board_slot = None
        self._refresh_buttons()
        self._refresh_hole_buttons()
        self._refresh_board_buttons()
        self._update_outputs()

    def _odds_cache_key(self, players: int, board_codes: list[str]) -> tuple[str, str, tuple[str, ...], int]:
        return (
            self.selected[0].code,
            self.selected[1].code,
            tuple(board_codes),
            players,
        )

    def _schedule_odds_update(self, players: int, board_codes: list[str]) -> None:
        if self.odds_after_id is not None:
            self.after_cancel(self.odds_after_id)
            self.odds_after_id = None

        self.odds_status_var.set(f"Odds vs {players - 1} opponents (calculating...)")
        self.win_var.set("Win: ...")
        self.tie_var.set("Tie: ...")
        self.loss_var.set("Loss: ...")
        self.equity_var.set("Total equity: ...")

        self.odds_after_id = self.after(120, lambda: self._run_odds_update(players, board_codes))

    def _hand_rank_cache_key(self, board_codes: list[str]) -> tuple[str, str, tuple[str, ...]]:
        return (
            self.selected[0].code,
            self.selected[1].code,
            tuple(board_codes),
        )

    def _schedule_hand_rank_update(self, board_codes: list[str]) -> None:
        if self.hand_rank_after_id is not None:
            self.after_cancel(self.hand_rank_after_id)
            self.hand_rank_after_id = None

        self.hand_rank_status_var.set("Hand odds: calculating your final hand distribution...")
        for key, _label in HAND_CATEGORY_ORDER:
            self.hand_rank_you_vars[key].set("...")
            self.hand_rank_other_vars[key].set("...")

        self.hand_rank_after_id = self.after(140, lambda: self._run_hand_rank_update(board_codes))

    def _run_hand_rank_update(self, board_codes: list[str]) -> None:
        self.hand_rank_after_id = None
        key = self._hand_rank_cache_key(board_codes)
        if key in self.hand_rank_cache:
            hero_rates, other_rates = self.hand_rank_cache[key]
        else:
            try:
                hero_rates, other_rates = simulate_hand_rank_distribution(
                    [self.selected[0].code, self.selected[1].code],
                    board_codes,
                    simulations=1800,
                )
            except ValueError as error:
                self.hand_rank_status_var.set(f"Hand odds: {error}")
                for rank_key, _label in HAND_CATEGORY_ORDER:
                    self.hand_rank_you_vars[rank_key].set("-")
                    self.hand_rank_other_vars[rank_key].set("-")
                return
            self.hand_rank_cache[key] = (hero_rates, other_rates)

        for rank_key, _label in HAND_CATEGORY_ORDER:
            self.hand_rank_you_vars[rank_key].set(f"{hero_rates[rank_key] * 100:.2f}%")
            self.hand_rank_other_vars[rank_key].set(f"{other_rates[rank_key] * 100:.2f}%")

        self.hand_rank_status_var.set("You = your final hand by river. Others = one random opponent with the same exposed board information.")

    def _run_odds_update(self, players: int, board_codes: list[str]) -> None:
        self.odds_after_id = None
        key = self._odds_cache_key(players, board_codes)
        if key in self.odds_cache:
            win, tie, loss = self.odds_cache[key]
        else:
            try:
                win, tie, loss = simulate_equity(
                    [self.selected[0].code, self.selected[1].code],
                    board_codes,
                    players,
                    simulations=1200,
                )
            except ValueError as error:
                self.odds_status_var.set(f"Odds: {error}")
                self.win_var.set("Win: -")
                self.tie_var.set("Tie: -")
                self.loss_var.set("Loss: -")
                self.equity_var.set("Total equity: -")
                if self.equity_label is not None:
                    self.equity_label.configure(fg=FG_MAIN)
                self._update_strategy_panel()
                return
            self.odds_cache[key] = (win, tie, loss)

        equity = win + tie
        self.odds_status_var.set(f"Odds vs {players - 1} opponents")
        self.win_var.set(f"Win: {win * 100:.1f}%")
        self.tie_var.set(f"Tie: {tie * 100:.1f}%")
        self.loss_var.set(f"Loss: {loss * 100:.1f}%")
        self.equity_var.set(f"Total equity: {equity * 100:.1f}%")
        if self.equity_label is not None:
            self.equity_label.configure(fg=self._score_color(int(equity * 100)))
        self._update_strategy_panel()

    def _set_board_slot(self, slot: str, card: Card | None) -> None:
        self.board_cards[slot] = card
        self.active_board_slot = slot if card is None else self._next_open_board_slot()
        if len(self.selected) == 2 and self.active_board_slot is not None:
            self.odds_note_var.set(f"Choose {BOARD_LABELS[self.active_board_slot]} from the main card grid.")
        self._refresh_board_buttons()
        self._refresh_buttons()
        self._update_outputs()

    def _change_players(self, delta: int) -> None:
        players = max(2, min(10, self.players_var.get() + delta))
        self.players_var.set(players)
        self._update_outputs()
        self._update_strategy_panel()

    def _update_outputs(self, replaced: bool = False) -> None:
        if len(self.selected) < 2:
            self.active_board_slot = None
            if self.odds_after_id is not None:
                self.after_cancel(self.odds_after_id)
                self.odds_after_id = None
            if self.hand_rank_after_id is not None:
                self.after_cancel(self.hand_rank_after_id)
                self.hand_rank_after_id = None
            self.combo_var.set("Hand: -")
            self.score_var.set("Score: -")
            self.tier_var.set("Tier: -")
            self.advice_var.set("Advice: Select two hole cards")
            self.current_hand_var.set("Current hand: -")
            self.odds_note_var.set("Select two hole cards to unlock board selection.")
            self.odds_status_var.set("Odds: select 2 hole cards")
            self.win_var.set("Win: -")
            self.tie_var.set("Tie: -")
            self.loss_var.set("Loss: -")
            self.equity_var.set("Total equity: -")
            self.hand_rank_status_var.set("Hand odds: select 2 hole cards")
            for key, _label in HAND_CATEGORY_ORDER:
                self.hand_rank_you_vars[key].set("-")
                self.hand_rank_other_vars[key].set("-")
            if self.score_label is not None:
                self.score_label.configure(fg=FG_MAIN)
            if self.equity_label is not None:
                self.equity_label.configure(fg=FG_MAIN)
            return

        result = evaluate_preflop(self.selected[0].code, self.selected[1].code)
        self.combo_var.set(f"Hand: {self._display_hand_key(result.hand_key)}")
        self.score_var.set(f"Score: {result.score}/100")
        self.tier_var.set(f"Tier: {result.tier}")
        board_codes = [
            self.board_cards["flop_1"].code if self.board_cards["flop_1"] is not None else "",
            self.board_cards["flop_2"].code if self.board_cards["flop_2"] is not None else "",
            self.board_cards["flop_3"].code if self.board_cards["flop_3"] is not None else "",
            self.board_cards["turn"].code if self.board_cards["turn"] is not None else "",
            self.board_cards["river"].code if self.board_cards["river"] is not None else "",
        ]
        self.current_hand_var.set(
            "Current hand: "
            + _describe_current_hand_context(
                [self.selected[0].code, self.selected[1].code],
                board_codes,
            )
        )
        prefix = "Replaced oldest hole card. " if replaced else ""
        self.advice_var.set(f"Advice: {prefix}{result.advice} ({result.reason})")
        if self.score_label is not None:
            self.score_label.configure(fg=self._score_color(result.score))
        if self.active_board_slot is None:
            self.active_board_slot = self._next_open_board_slot()
        if self.active_board_slot is not None:
            self.odds_note_var.set(f"Choose {BOARD_LABELS[self.active_board_slot]} from the main card grid.")
        else:
            self.odds_note_var.set("Board complete. Use the board buttons to replace a street if needed.")
        self._refresh_board_buttons()

        players = max(2, min(10, self.players_var.get()))
        self.players_var.set(players)

        self._schedule_odds_update(players, board_codes)
        self._schedule_hand_rank_update(board_codes)
        self._update_strategy_panel()


def main() -> None:
    use_custom_chrome = "--standard-window" not in sys.argv
    print("Starting Poker Hand Trainer...", flush=True)
    print(
        "Using custom borderless window chrome." if use_custom_chrome else "Using standard OS window chrome.",
        flush=True,
    )
    try:
        app = PreflopApp(use_custom_chrome=use_custom_chrome)
        print("Poker Hand Trainer window initialized.", flush=True)
        app.mainloop()
    except Exception:
        print("Poker Hand Trainer failed to start:", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
