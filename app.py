from __future__ import annotations

import json
import re
import sqlite3
import threading
import sys
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

from bridge_payloads import parse_bridge_payload
from handranker import (
    HAND_CATEGORY_ORDER,
    RANKS_DESC,
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


SUPPORTED_BRIDGE_SITES = (
    BridgeSite(
        key="casino_org_replaypoker",
        label="casino.org/replaypoker",
        url="https://casino.org/replaypoker",
        tracker_site="ReplayPoker",
    ),
)
DEFAULT_BRIDGE_SITE = SUPPORTED_BRIDGE_SITES[0]
BRIDGE_SITES_BY_LABEL = {site.label: site for site in SUPPORTED_BRIDGE_SITES}


class PreflopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self._use_custom_chrome = True
        self.title("Poker Hand Trainers by Cha0i")
        self.geometry("900x1600")
        self.minsize(760, 520)
        self.resizable(True, True)
        self.configure(bg=BG_MAIN, highlightthickness=0, bd=0)
        self.option_add("*HighlightThickness", 0)
        if self._use_custom_chrome:
            self.overrideredirect(True)
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
        self._bridge_available = _BRIDGE_DEPENDENCIES_AVAILABLE
        self.site_var.trace_add("write", self._handle_site_changed)

        self.board_cards: dict[str, Card | None] = {
            "flop_1": None,
            "flop_2": None,
            "flop_3": None,
            "turn": None,
            "river": None,
        }

        if self._use_custom_chrome:
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
        self.after(120, self._ensure_window_visible)

    def _ensure_window_visible(self) -> None:
        if not self.winfo_exists():
            return
        try:
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
        self._build_resize_grips(shell)

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
        return normalized.startswith("high card")

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
        if "AIR" in normalized_headline and not high_card_only:
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
            "hole": [card.code for card in self.selected],
            "board": [card.code for card in self.board_cards.values() if card is not None],
            "advice_headline": self._strategy_headline(),
            "advice_recommendation": self._strategy_recommendation(),
        }
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

        @app.post("/log")
        def receive_log() -> tuple[str, int]:
            line = (request.get_data(as_text=True) or "").strip()
            if line:
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

        try:
            self._server = make_server("127.0.0.1", 5000, app)
        except OSError as error:
            self._set_server_status("Bridge: failed to bind 127.0.0.1:5000", False)
            self.server_info_var.set(f"Error: {error}")
            self._append_server_log(f"[bridge] startup failed: {error}")
            return

        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()
        self._server_running = True
        self._set_server_status("Bridge: online on 127.0.0.1:5000", True)
        self.server_info_var.set(self._bridge_waiting_info())
        if self.server_toggle_button is not None:
            self.server_toggle_button.configure(text="Stop Server")
        self._append_server_log("[bridge] server started on http://127.0.0.1:5000/log")

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

    def _parse_tagged_bridge_payload(
        self,
        payload: object,
    ) -> tuple[list[str], list[str] | None, int | None, bool, int | None, int | None, int | None, bool | None] | None:
        parsed = parse_bridge_payload(payload)
        if parsed is None:
            return None
        return (
            parsed.hole_cards,
            parsed.board_cards,
            parsed.players_count,
            parsed.reset_state,
            parsed.hand_id,
            parsed.hero_user_id,
            parsed.hero_seat_id,
            parsed.hero_sitting_out,
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
        hole_cards, board_cards, players_count, reset_state, hand_id, hero_user_id, hero_seat_id, hero_sitting_out = parsed
        self._apply_external_cards(
            hole_cards,
            board_cards,
            players_count,
            reset_state,
            hand_id=hand_id,
            hero_user_id=hero_user_id,
            hero_seat_id=hero_seat_id,
            hero_sitting_out=hero_sitting_out,
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
            return "Low"
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

        self.training_status_var.set("Training: active")

        preflop = evaluate_preflop(self.selected[0].code, self.selected[1].code)
        equity = self._equity_fraction()
        to_call = self._to_call_chips if isinstance(self._to_call_chips, int) else 0
        pot = self._pot_chips if isinstance(self._pot_chips, int) else 0
        pot_odds = (to_call / (pot + to_call)) if (to_call > 0 and pot + to_call > 0) else None
        board_count = sum(1 for card in self.board_cards.values() if card is not None)
        board_codes = [card.code for card in self.board_cards.values() if card is not None]
        made_hand = describe_current_hand([self.selected[0].code, self.selected[1].code], board_codes) if board_count >= 3 else None
        made_hand_lower = made_hand.lower() if isinstance(made_hand, str) else ""
        paired_board = len(board_codes) != len({code[0] for code in board_codes}) if board_codes else False
        suit_counts: dict[str, int] = {}
        for code in board_codes:
            suit_counts[code[1]] = suit_counts.get(code[1], 0) + 1
        max_suit_count = max(suit_counts.values()) if suit_counts else 0
        flush_heavy_board = max_suit_count >= 3
        overbet_pressure = pot > 0 and to_call >= max(1, pot)
        large_bet_pressure = pot > 0 and to_call >= max(1, int(pot * 0.6))
        weak_action = aggression == "Low" and to_call == 0
        min_raise = self._minimum_raise_chips if isinstance(self._minimum_raise_chips, int) and self._minimum_raise_chips > 0 else 2
        preflop_shove_threshold = max(12, min_raise * 8)
        preflop_allin_pressure = self._street == "preflop" and (
            self._allin_pressure
            or to_call >= preflop_shove_threshold
            or (to_call >= max(1, pot) and pot >= preflop_shove_threshold // 2)
        )

        headline = "PLAY SOLID."
        details: list[str] = []

        if self._street == "preflop":
            if self._hero_acted_preflop and to_call == 0:
                headline = "PRE-FLOP DECISION MADE. WAIT FOR FLOP."
                details.append("You already acted preflop and there is no new price to call. Let the flop arrive before changing plans.")
            elif preflop_allin_pressure and to_call > 0:
                if preflop.score >= 82:
                    headline = "PREFLOP SHOVE DETECTED. CALL OR RE-JAM."
                    details.append("This is strong enough to continue against most all-in ranges.")
                elif equity is not None and pot_odds is not None and equity >= pot_odds + 0.08 and preflop.score >= 65:
                    headline = "PREFLOP SHOVE DETECTED. CALL CAREFULLY."
                    details.append(
                        f"Exploitative call: equity about {self._format_percent(equity)} vs required {self._format_percent(pot_odds)}."
                    )
                else:
                    headline = "PREFLOP SHOVE DETECTED. FOLD MOST MARGINAL HANDS."
                    if equity is not None and pot_odds is not None:
                        details.append(
                            f"Do not punt: equity about {self._format_percent(equity)} vs required {self._format_percent(pot_odds)}."
                        )
                    details.append("Population preflop jams are under-bluffed at these stakes, so fold weak offsuit broadway/trash.")
            elif preflop.score >= 75:
                headline = "MONSTER HAND. BET BIG."
                details.append("Premium preflop range. Population calls too much here, so print value immediately.")
            elif preflop.score >= 60:
                headline = "GOOD HAND. RAISE."
                details.append("You are ahead of the junk people show up with. Raise and take control instead of inviting nonsense in.")
            elif preflop.score >= 45:
                if to_call > 0:
                    headline = "MARGINAL HAND. USUALLY FOLD."
                    details.append("This is exactly the kind of hand people convince themselves to peel with and regret later.")
                else:
                    headline = "PLAYABLE HAND. OPEN SMALL."
                    details.append("If you are first in, make a small open. Do not bloat the pot with junk from bad position.")
            else:
                headline = "TRASH HAND. FOLD."
                details.append("Without a real steal spot, this is not worth opening.")
        else:
            if board_count == 5 and to_call > 0 and self._is_high_card_only(made_hand_lower):
                headline = "RIVER AIR. FOLD."
                if made_hand:
                    details.append(f"Your made hand is only {made_hand.lower()}. Do not pay off with air just because the price looks close.")
            elif board_count == 5 and to_call == 0 and self._is_high_card_only(made_hand_lower):
                headline = "RIVER AIR. CHECK."
                if made_hand:
                    details.append(f"Your made hand is only {made_hand.lower()}. Take the free card/showdown instead of forcing action.")
            elif equity is not None and equity >= 0.75:
                headline = "MONSTER HAND. BET BIG."
                details.append("You are way ahead of one-pair nonsense. Stop trapping and start charging.")
            elif equity is not None and equity >= 0.55 and aggression == "Low":
                headline = "STRONG HAND. CHECK OR BET SMALL."
                details.append("Nobody seems to have much. Overbetting just folds out worse hands, so keep them in with a small size.")
            elif equity is not None and equity >= 0.55:
                headline = "VALUE HAND. BET SMALL TO MEDIUM."
                details.append("You are probably good, but the action says this is not a spot to torch stacks with one pair.")

            if board_count == 5 and to_call == 0 and equity is not None:
                if equity >= 0.7:
                    headline = "VALUE HAND. BET SMALL TO MEDIUM."
                    details.append("You have a real river value spot. Bet small to get paid by worse one-pair hands and bluff-catchers.")
                elif equity >= 0.5:
                    headline = "SHOWDOWN VALUE. BET SMALL OR CHECK."
                    details.append("Thin river value is available, but keep sizes honest and do not force a huge pot.")

            if pot_odds is not None and equity is not None:
                if equity < pot_odds - 0.08 and to_call > 0:
                    headline = "THEY BET BIG, YOU GOT SHIT. FOLD."
                    details.append(
                        f"Your equity is about {self._format_percent(equity)} and pot odds need about {self._format_percent(pot_odds)}."
                    )
                elif abs(equity - pot_odds) <= 0.06 and to_call > 0:
                    headline = "BLUFF CATCHER. MAYBE CALL, MAYBE FOLD."
                    details.append(
                        f"Close spot: equity about {self._format_percent(equity)} vs pot odds about {self._format_percent(pot_odds)}."
                    )
                    details.append("Live-read version: if this sizing feels weird and value-heavy, fold more. If it looks stabby, flick in the call.")
                elif equity > pot_odds + 0.12 and to_call > 0:
                    headline = "YOU'RE AHEAD. CALL OR RAISE SMALL."
                    details.append(
                        f"Math is on your side: equity about {self._format_percent(equity)} vs pot odds about {self._format_percent(pot_odds)}."
                    )
                elif equity >= pot_odds and to_call > 0:
                    headline = "PRICE IS OKAY. CALL ONLY WITH REAL SHOWDOWN VALUE."
                    details.append(
                        f"You are getting roughly the right price: equity about {self._format_percent(equity)}."
                    )
                else:
                    details.append("Pot odds do not justify paying off without stronger showdown value.")
            elif to_call > 0 and aggression == "High":
                headline = "THEY'RE PUSHING HARD. FOLD MOST TRASH."
                details.append("Without a strong made hand or a real draw, this is a bad hero spot.")

            if board_count >= 3 and equity is not None and equity < 0.35 and to_call == 0:
                headline = "NOT MUCH THERE. CHECK."
                details.append("No reason to torch chips with weak showdown value into a developed board.")

            if weak_action and board_count < 5 and equity is not None and equity >= 0.45 and headline not in {"MONSTER HAND. BET BIG.", "VALUE HAND. BET SMALL TO MEDIUM.", "YOU'RE AHEAD. CHECK OR BET SMALL."}:
                headline = "NOBODY SEEMS TO HAVE SHIT. STAB SMALL OR CHECK."
                details.append("This line looks capped. A small stab works often, but checking back is fine if your hand has showdown value.")

            if overbet_pressure:
                details.append("Overbet pressure is population-biased toward nutted hands or obvious panic bluffs. Continue only with real equity or clear bluff-catchers.")
            elif large_bet_pressure:
                details.append("Big sizing usually means they want folds. Respect it more on wet boards and call lighter on disconnected boards.")

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
            f"EQ {self._format_percent(equity)} | ODDS {self._format_percent(pot_odds)}"
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
        hero_user_id: int | None = None,
        hero_seat_id: int | None = None,
        hero_sitting_out: bool | None = None,
    ) -> None:
        if self._hero_folded_waiting_for_new_hole and not hole_cards:
            self._update_strategy_panel()
            return

        accepting_fresh_hole = self._hero_folded_waiting_for_new_hole or self._awaiting_hero_hole_after_reset

        self._log_app_action(
            "apply_external_cards_start",
            hole_cards=hole_cards,
            board_cards=board_cards,
            players_count=players_count,
            reset_state=reset_state,
            hand_id=hand_id,
            hero_user_id=hero_user_id,
            hero_seat_id=hero_seat_id,
            hero_sitting_out=hero_sitting_out,
        )

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
            self._strategy_players_count = None
            self._recent_actions = []

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

            incoming_hole_codes = [card.code for card in parsed_hole]
            existing_hole_codes = [card.code for card in self.selected]

            # Keep hole cards immutable within a hand to prevent noisy payload overwrites.
            if board_count > 0 and len(existing_hole_codes) == 2 and not accepting_fresh_hole:
                self._append_server_log("[bridge] ignored hole update after board started")
                self._log_app_action(
                    "apply_external_cards_skip",
                    reason="hole_after_board_started",
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

        if players_count is not None:
            self.players_var.set(players_count)

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
        if not self._use_custom_chrome or self.state() != "normal":
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
        if self._use_custom_chrome:
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
            f"Current hand: {describe_current_hand([self.selected[0].code, self.selected[1].code], board_codes)}"
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
    app = PreflopApp()
    app.mainloop()


if __name__ == "__main__":
    main()
