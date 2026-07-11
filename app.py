from __future__ import annotations

import json
import re
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue

from flask import Flask, request
from werkzeug.serving import make_server

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


class PreflopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self._use_custom_chrome = True
        self.title("Poker Hand Trainers by Cha0i")
        self.geometry("1140x1860")
        self.minsize(560, 520)
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
        self.subtitle_label: tk.Label | None = None
        self.advice_label: tk.Label | None = None
        self.current_hand_label: tk.Label | None = None
        self.maximize_button: tk.Button | None = None
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
        self.strategy_context_label: tk.Label | None = None
        self.strategy_advice_label: tk.Label | None = None
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
        self.server_info_var = tk.StringVar(value="Send tagged JSON: TM_BRIDGE:{\"type\":\"poker_cards\",...}")
        self._server_running = False
        self._server: any = None
        self._server_thread: threading.Thread | None = None
        self._incoming_logs: Queue[str] = Queue()
        self._server_poll_job: str | None = None
        self._bridge_log_file = Path(__file__).resolve().parent / "browser-console.log"
        self._app_actions_log_file = Path(__file__).resolve().parent / "app-actions.log"
        self.strategy_context_var = tk.StringVar(value="Street: - | Pot: - | To call: - | Aggression: -")
        self.strategy_advice_var = tk.StringVar(
            value=(
                "Smart play coach active. Start the bridge to read live bets and action flow. "
                "Baseline strategy is tight-aggressive with pot-odds discipline."
            )
        )
        self._street = "preflop"
        self._hero_seat_id: int | None = None
        self._hero_user_id: int | None = None
        self._pot_chips: int | None = None
        self._to_call_chips: int | None = None
        self._strategy_players_count: int | None = None
        self._allin_pressure = False
        self._recent_actions: list[str] = []

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
        self._build_ui()
        self._refresh_buttons()
        self._refresh_hole_buttons()
        self._refresh_board_buttons()
        self._apply_scale()
        self._schedule_server_poll()

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

    def _build_ui(self) -> None:
        shell = tk.Frame(self, bg=BG_MAIN, highlightbackground="#27303d", highlightcolor="#27303d", highlightthickness=1, bd=0)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)

        self._build_title_bar(shell)
        self._build_resize_grips(shell)

        root = tk.Frame(shell, padx=10, pady=10, bg=BG_MAIN, highlightthickness=0, bd=0)
        root.grid(row=1, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1, uniform="top")
        root.columnconfigure(1, weight=2, uniform="top")
        root.columnconfigure(2, weight=1, uniform="top")
        root.rowconfigure(2, weight=5)
        root.rowconfigure(3, weight=4)

        tk.Label(
            root,
            text="Poker Hand Trainer by Cha0i",
            font=self.fonts["title"],
            bg=BG_MAIN,
            fg=FG_MAIN,
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self.subtitle_label = tk.Label(
            root,
            text="Remember to keep adjusting the player count to accurately calculate odds.",
            font=self.fonts["subtitle"],
            bg=BG_MAIN,
            fg=FG_MUTED,
            justify="left",
            anchor="w",
        )
        self.subtitle_label.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 8))

        grid_frame = tk.Frame(root, bd=1, relief="solid", padx=6, pady=6, bg=BG_PANEL, highlightbackground="#27303d", highlightcolor="#27303d", highlightthickness=1)
        grid_frame.grid(row=2, column=1, sticky="nsew", padx=(8, 8))
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
        self._build_strategy_column(root)

        info_row = tk.Frame(root, bg=BG_MAIN)
        info_row.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        info_row.columnconfigure(0, weight=3)
        info_row.columnconfigure(1, weight=2)
        info_row.columnconfigure(2, weight=2)
        info_row.rowconfigure(0, weight=1)
        self.info_row = info_row

        self._build_left_column(info_row)
        self._build_right_column(info_row)
        self._build_rank_column(info_row)

    def _build_server_column(self, root: tk.Frame) -> None:
        frame = tk.Frame(root, bg=BG_PANEL, padx=10, pady=10, highlightbackground="#27303d", highlightcolor="#27303d", highlightthickness=1)
        frame.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
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
        ).grid(row=0, column=1, padx=(6, 0), sticky="w")

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
        frame.grid(row=2, column=2, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        self.strategy_frame = frame

        tk.Label(frame, text="Smart Play Coach", font=self.fonts["section"], bg=BG_PANEL, fg=FG_MAIN).grid(row=0, column=0, sticky="w")
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
        self.strategy_context_label.grid(row=1, column=0, sticky="ew", pady=(6, 0))
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
        self.strategy_advice_label.grid(row=2, column=0, sticky="nsew", pady=(10, 0))

    def _clear_server_log(self) -> None:
        if self.server_log_widget is not None:
            self.server_log_widget.delete("1.0", tk.END)

    def _append_server_log(self, line: str) -> None:
        if self.server_log_widget is None:
            return
        self.server_log_widget.insert(tk.END, f"{line}\n")
        self.server_log_widget.see(tk.END)

    def _log_app_action(self, event: str, **fields: object) -> None:
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
        self.server_info_var.set("Waiting for tagged bridge lines...")
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
            self.server_info_var.set("Send tagged JSON: TM_BRIDGE:{\"type\":\"poker_cards\",...}")
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

    def _parse_tagged_bridge_payload(self, payload: object) -> tuple[list[str], list[str] | None, int | None, bool] | None:
        if not isinstance(payload, dict):
            return None
        if payload.get("type") != "poker_cards":
            return None

        has_hole = "hole" in payload
        has_board = "board" in payload
        has_players = "players" in payload
        has_reset = "reset" in payload
        if not has_hole and not has_board and not has_players and not has_reset:
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

        return hole_cards, board_cards, players_count, reset_state

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
        hole_cards, board_cards, players_count, reset_state = parsed
        self._apply_external_cards(hole_cards, board_cards, players_count, reset_state)

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

    def _update_strategy_from_update(self, update: dict[str, object]) -> None:
        action = str(update.get("action", "")).strip()
        if not action:
            return

        action_lower = action.lower()
        self._recent_actions.append(action_lower)
        if len(self._recent_actions) > 24:
            self._recent_actions = self._recent_actions[-24:]

        state_value = update.get("state")
        if isinstance(state_value, str):
            normalized = state_value.lower()
            if normalized in {"preflop", "flop", "turn", "river"}:
                self._street = normalized

        if action_lower == "starthand":
            self._street = "preflop"
            self._pot_chips = None
            self._to_call_chips = None
            self._recent_actions = []
            self._allin_pressure = False

        players = update.get("players")
        players_count = self._players_active_from_snapshot(players)
        if players_count is not None:
            self._strategy_players_count = players_count

        if isinstance(players, list):
            for player in players:
                if not isinstance(player, dict):
                    continue
                cards = player.get("cards")
                if isinstance(cards, list) and len(cards) == 2:
                    shown = [str(card).upper() for card in cards]
                    if all(card in {"X", "XX"} for card in shown):
                        continue
                    if all(re.fullmatch(r"[2-9TJQKA][CDHS]", card) for card in shown):
                        seat = player.get("seatId")
                        if isinstance(seat, int):
                            self._hero_seat_id = seat
                        user_id = player.get("userId")
                        if isinstance(user_id, int):
                            self._hero_user_id = user_id

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
        if isinstance(minimum_raise, int) and minimum_raise >= 0 and self._to_call_chips is None:
            self._to_call_chips = minimum_raise

        if action_lower == "awardpot":
            self._to_call_chips = 0
            self._allin_pressure = False

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

        updates = payload.get("updates")
        if isinstance(updates, list):
            for update in updates:
                if isinstance(update, dict):
                    self._update_strategy_from_update(update)
        elif isinstance(payload.get("action"), str):
            self._update_strategy_from_update(payload)

        self._update_strategy_panel()
        self._log_app_action("strategy_line_processed")

    def _update_strategy_panel(self) -> None:
        street = self._street_label()
        pot_text = str(self._pot_chips) if isinstance(self._pot_chips, int) else "-"
        to_call_text = str(self._to_call_chips) if isinstance(self._to_call_chips, int) else "-"
        aggression = self._aggression_label()
        self.strategy_context_var.set(f"Street: {street} | Pot: {pot_text} | To call: {to_call_text} | Aggression: {aggression}")

        players_source = self._strategy_players_count if self._strategy_players_count is not None else self.players_var.get()
        players = max(2, min(10, players_source))

        if len(self.selected) < 2:
            self.strategy_advice_var.set(
                "WAIT FOR HOLE CARDS.\n"
                "No decision yet. Once your two cards arrive, the coach will give a direct action line first and short reasoning underneath."
            )
            return

        preflop = evaluate_preflop(self.selected[0].code, self.selected[1].code)
        equity = self._equity_fraction()
        to_call = self._to_call_chips if isinstance(self._to_call_chips, int) else 0
        pot = self._pot_chips if isinstance(self._pot_chips, int) else 0
        pot_odds = (to_call / (pot + to_call)) if (to_call > 0 and pot + to_call > 0) else None
        board_count = sum(1 for card in self.board_cards.values() if card is not None)
        board_codes = [card.code for card in self.board_cards.values() if card is not None]
        paired_board = len(board_codes) != len({code[0] for code in board_codes}) if board_codes else False
        suit_counts: dict[str, int] = {}
        for code in board_codes:
            suit_counts[code[1]] = suit_counts.get(code[1], 0) + 1
        max_suit_count = max(suit_counts.values()) if suit_counts else 0
        flush_heavy_board = max_suit_count >= 3
        overbet_pressure = pot > 0 and to_call >= max(1, pot)
        large_bet_pressure = pot > 0 and to_call >= max(1, int(pot * 0.6))
        weak_action = aggression == "Low" and to_call == 0
        preflop_allin_pressure = self._street == "preflop" and (self._allin_pressure or overbet_pressure or large_bet_pressure)

        headline = "PLAY SOLID."
        details: list[str] = []

        if self._street == "preflop":
            if preflop_allin_pressure and to_call > 0:
                if preflop.score >= 82:
                    headline = "PREMIUM VS JAM. CALL OR RE-JAM."
                    details.append("This is strong enough to continue against most all-in ranges.")
                elif equity is not None and pot_odds is not None and equity >= pot_odds + 0.08 and preflop.score >= 65:
                    headline = "PRICE IS GOOD. CALL CAREFULLY."
                    details.append(
                        f"Exploitative call: equity about {self._format_percent(equity)} vs required {self._format_percent(pot_odds)}."
                    )
                else:
                    headline = "THEY JAMMED. K6-TYPE HANDS FOLD."
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
                    headline = "PLAYABLE HAND. OPEN SMALL OR FOLD."
                    details.append("Fine to steal with late or short-handed, but do not build a dumb pot from early position.")
            else:
                headline = "SHIT HAND. FOLD."
                details.append("Trash is trash. Do not invent reasons to continue just because nobody has shown strength yet.")
        else:
            if equity is not None and equity >= 0.75:
                headline = "MONSTER HAND. BET BIG."
                details.append("You are way ahead of one-pair nonsense. Stop trapping and start charging.")
            elif equity is not None and equity >= 0.55 and aggression == "Low":
                headline = "YOU'RE AHEAD. CHECK OR BET SMALL."
                details.append("Nobody seems to have much. Overbetting just folds out worse hands, so keep them in with a small size.")
            elif equity is not None and equity >= 0.55:
                headline = "VALUE HAND. BET SMALL TO MEDIUM."
                details.append("You are probably good, but the action says this is not a spot to torch stacks with one pair.")

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
                    headline = "CALL IS FINE. DON'T GET FANCY."
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

            if weak_action and equity is not None and equity >= 0.45 and headline not in {"MONSTER HAND. BET BIG.", "VALUE HAND. BET SMALL TO MEDIUM.", "YOU'RE AHEAD. CHECK OR BET SMALL."}:
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

        self.strategy_advice_var.set(headline + "\n" + " ".join(details))

    def _card_from_code(self, code: str) -> Card | None:
        normalized = code.strip().upper()
        if len(normalized) != 2:
            return None
        rank = normalized[0]
        suit = normalized[1].lower()
        if rank not in RANKS_DESC or suit not in SUIT_SYMBOLS:
            return None
        return Card(rank=rank, suit=suit)

    def _apply_external_cards(self, hole_cards: list[str], board_cards: list[str] | None, players_count: int | None = None, reset_state: bool = False) -> None:
        self._log_app_action(
            "apply_external_cards_start",
            hole_cards=hole_cards,
            board_cards=board_cards,
            players_count=players_count,
            reset_state=reset_state,
        )
        parsed_hole: list[Card] = []
        parsed_board: list[Card] = []

        if reset_state:
            self.selected = []
            for slot in BOARD_ORDER:
                self.board_cards[slot] = None
            self._street = "preflop"
            self._pot_chips = None
            self._to_call_chips = None
            self._strategy_players_count = None
            self._recent_actions = []

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
            board_count = sum(1 for card in self.board_cards.values() if card is not None)

            # Keep hole cards immutable within a hand to prevent noisy payload overwrites.
            if board_count > 0:
                self._append_server_log("[bridge] ignored hole update after board started")
                self._log_app_action(
                    "apply_external_cards_skip",
                    reason="hole_after_board_started",
                    incoming_hole=incoming_hole_codes,
                    existing_hole=existing_hole_codes,
                )
                parsed_hole = []
            elif len(existing_hole_codes) == 2 and incoming_hole_codes != existing_hole_codes:
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
        hole_changed = bool(parsed_hole) and [card.code for card in self.selected] != previous_hole_codes
        if board_cards is not None:
            if len(parsed_board) == 0:
                for slot in BOARD_ORDER:
                    self.board_cards[slot] = None

            existing_board_cards = [self.board_cards[slot] for slot in BOARD_ORDER if self.board_cards[slot] is not None]

            if hole_changed:
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
        self._update_strategy_panel()

    def destroy(self) -> None:
        if self._server_poll_job is not None:
            try:
                self.after_cancel(self._server_poll_job)
            except tk.TclError:
                pass
            self._server_poll_job = None
        self._stop_server()
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

        buttons = tk.Frame(bar, bg=BG_PANEL)
        buttons.grid(row=0, column=1, sticky="e")

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
        ).grid(row=0, column=0, padx=(0, 1), pady=1)

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
        self.maximize_button.grid(row=0, column=1, padx=1, pady=1)

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
        ).grid(row=0, column=2, padx=(1, 4), pady=1)

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
        height = max(self.winfo_height(), 520)
        scale = min(width / 920, height / 660)
        scale = max(0.9, min(1.35, scale))
        narrow = width < 760

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

        if self.subtitle_label is not None:
            self.subtitle_label.configure(wraplength=max(260, width - 80))
        panel_wrap = max(120, int((width - 84) / 3) - 20)
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