# Poker Hand Trainer by Cha0i

A desktop poker training app that combines manual card selection with live browser hand ingestion, equity simulation, and strategy guidance.

This project is nearly completely generated with AI. 

## What This App Does

- Lets you set hero hole cards and community cards in a desktop UI.
- Simulates win/tie/loss equity against a configurable number of players.
- Shows hand-category distribution odds.
- Optionally runs a local browser bridge server for the selected poker site. Current supported targets are `casino.org/replaypoker` and `unibet.nl/pokerwebclient`.
- Logs advice and outcomes for strategy review.

## Features

- Interactive card grid with duplicate-card protection.
- Street-aware strategy coach (preflop, flop, turn, river).
- Pot/to-call/action-aware recommendations.
- Browser bridge via tagged payloads (`TM_BRIDGE:{...}`).
- Site selector backed by a bridge-site catalog.
- Training/event logs for post-session analysis.

## Project Structure

- `app.py`: Tkinter app, bridge server, strategy panel, and logging.
- `bridge_payloads.py`: browser bridge payload schema parsing.
- `handranker.py`: hand evaluation and simulation logic.
- `tampermonkey-bridge.user.js`: browser userscript that sends tagged updates.
- `strategy-training.log`: JSONL training and outcome events.
- `app-actions.log`: app-side ingest/apply trace.
- `browser-console.log`: captured bridge-related browser log lines.

## Requirements

- Python 3.10 or newer
- pip
- Tkinter available in your Python installation
- If using the browser bridge, Tampermonkey in Firefox or Chrome. Unibet's poker client requires Chrome.

## Installation

1. Clone the repository.
2. Create a virtual environment.
3. Activate the virtual environment.
4. Install dependencies from `requirements.txt`.

### Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Windows (PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Run

From the project root:

```bash
python app.py
```

## Browser Bridge Setup (Tampermonkey)

1. Install Tampermonkey for your browser. Use Chrome for Unibet; ReplayPoker can use Firefox or Chrome.
2. Start the app bridge, then open `http://127.0.0.1:5000/tampermonkey-bridge.user.js` in Chrome and install it through Tampermonkey. This canonical local URL includes update metadata, avoiding detached imported copies.
3. Launch the app and choose a site from the selector.
4. Click **Start Server**.
5. Confirm the userscript posts to `http://127.0.0.1:5000/log`.
6. Use the bridge log panel in the app to confirm payloads are arriving.

The userscript is currently scoped to `casino.org` and Unibet Netherlands pages. It keeps raw console mirroring off for casino.org and captures the Relax Gaming transport used by Unibet. During Unibet validation, `browser-console.log` includes iframe URLs, WebSocket messages, `fetch` summaries, and XHR summaries alongside decoded `TM_BRIDGE` payloads.

For Unibet in Chrome, verify Tampermonkey shows userscript version `2.3`, close all existing Unibet tabs, and reopen the poker client. Look for `[PokerOdds Bridge v2.3]` in DevTools Console. Version `2.3` runs inside the Relax Gaming poker-client iframe, captures its WebSocket traffic, and decodes the compact XMPP hand messages into normal `TM_BRIDGE` updates for hand ID, hero seat and hole cards, community cards, active-player count, pot size, call price, minimum raise, and hero-turn timing. Unibet updates repeat the known hole and community cards as a self-contained snapshot, allowing the desktop app to recover its card state after an app restart or a missed bridge request. Its WebSocket discovery hooks both the page realm and listener APIs so inbound frames are captured even when the game bypasses the wrapped constructor. XMPP authentication payloads and common credential fields are redacted before logging. Every discovery record includes its page and frame context. The outer Unibet page only performs iframe and cross-frame message discovery, avoiding unrelated account-response bodies. Keep exactly one copy of the userscript enabled in Tampermonkey. The app rejects legacy Unibet discovery lines that lack page context and reports the canonical installation URL in its bridge log. You can also open the Tampermonkey extension menu on the Unibet tab and click **Test PokerOdds bridge**; the app should receive a versioned `[BRIDGE_DEBUG] manual bridge test` line.

### Adding More Sites

Site support starts in `SUPPORTED_BRIDGE_SITES` in `app.py`. Add a `BridgeSite` entry with a display label, target URL, and tracker site name. If the new site needs browser ingestion, also add the matching Tampermonkey `@match` rules and extraction logic in `tampermonkey-bridge.user.js`.

Example payload format:

```json
TM_BRIDGE:{"type":"poker_cards","hole":["As","Kd"],"board":["7c","2d","Th"],"players":4}
```

Supported fields:
- `type`: must be `"poker_cards"`
- `hole`: exactly 2 cards
- `board`: 0 to 5 cards
- `players`: integer (app clamps to 2..10)
- `reset`: boolean
- `handId`: numeric hand identifier
- `heroSeatId`: numeric hero seat index
- `heroSittingOut`: boolean
- `heroFolded`: boolean
- `pot`: current total pot in table chips
- `toCall`: current hero call price
- `minimumRaise`: minimum raise-to amount when available
- `heroTurn`: boolean indicating whether action is currently on the hero

## Logs and Analysis

The strategy coach is a live heuristic, not a GTO solver. On Unibet it waits for the hero turn and uses the detected cards, street, active-player count, pot, call price, minimum raise, and simulated equity. Postflop advice distinguishes made hands, board-only pairs, immediate straight/flush draws, and high-card hands. When facing a bet it adds a conservative range buffer above raw pot odds, and draw calls use next-card draw odds instead of equity against a completely random hand. Position, effective stack depth, and exact opponent ranges are not yet decoded, so close decisions remain conservative training guidance.

- `strategy-training.log`
  - JSONL records like `hand_start`, `advice_snapshot`, `hero_action`, and `hand_end`.
  - Advice snapshots include `random_equity` and `pot_odds`; random equity describes performance against random cards, not the stronger range implied by a bet.
  - Useful for comparing recommendations with outcomes.

- `app-actions.log`
  - App-side event processing trace.
  - Useful when validating update ordering and data application.

- `browser-console.log`
  - Captured browser-side bridge lines.
  - Useful to verify hole/board payload emission.

## Troubleshooting

- Bridge is offline
  - Make sure the app server is started in the UI.
  - Make sure no other process is using port 5000.

- App starts but no window appears
  - The app uses its custom borderless, resizable window by default.
  - Run `python app.py --standard-window` only as an emergency fallback if the desktop refuses to map the custom window.
  - Check the console for the startup traceback now printed by `app.py`.

- Repeated `WAIT FOR HOLE CARDS`
  - First check whether Unibet actually dealt the hero into that hand. Folded/not-participating and sitting-out seat states legitimately have no `deal` frame or hole cards.
  - If the hero was dealt in, compare the Relax `deal` frame, the following `TM_BRIDGE` `hole` payload in `browser-console.log`, and the matching `hole_set` event in `strategy-training.log`.

- Advice does not match current state
  - Compare event sequence in `app-actions.log` and `strategy-training.log`.
  - Confirm the userscript is version 2.3, and that new advice records contain the correct street plus numeric `pot` and `to_call` values.

## Development Check

Quick syntax validation:

```bash
python -m py_compile app.py handranker.py bridge_payloads.py
node --check tampermonkey-bridge.user.js
node tests/test_relax_userscript.js
python -m unittest discover -s tests
```

## License

No license file is currently included in this repository.
