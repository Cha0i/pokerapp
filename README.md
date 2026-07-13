# Poker Hand Trainer by Cha0i

A desktop poker training app that combines manual card selection with live browser hand ingestion, equity simulation, and strategy guidance.

This project is nearly completely generated with co-pilot. 

## What This App Does

- Lets you set hero hole cards and community cards in a desktop UI.
- Simulates win/tie/loss equity against a configurable number of players.
- Shows hand-category distribution odds.
- Opionally runs a local browser bridge server on https://casino.org/replay poker and automatically picks cards.s
- Logs advice and outcomes for strategy review.

## Features

- Interactive card grid with duplicate-card protection.
- Street-aware strategy coach (preflop, flop, turn, river).
- Pot/to-call/action-aware recommendations.
- Browser bridge via tagged payloads (`TM_BRIDGE:{...}`).
- Training/event logs for post-session analysis.

## Project Structure

- `app.py`: Tkinter app, bridge server, strategy panel, and logging.
- `handranker.py`: hand evaluation and simulation logic.
- `tampermonkey-bridge.user.js`: browser userscript that sends tagged updates.
- `strategy-training.log`: JSONL training and outcome events.
- `app-actions.log`: app-side ingest/apply trace.
- `browser-console.log`: captured bridge-related browser log lines.

## Requirements

- Python 3.10 or newer
- pip
- Tkinter available in your Python installation

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

1. Install Tampermonkey in your browser.
2. Import `tampermonkey-bridge.user.js` as a new script.
3. Launch the app and click **Start Server**.
4. Confirm the userscript posts to `http://127.0.0.1:5000/log`.
5. Use the bridge log panel in the app to confirm payloads are arriving.

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

## Logs and Analysis

- `strategy-training.log`
  - JSONL records like `hand_start`, `advice_snapshot`, `hero_action`, and `hand_end`.
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

- Repeated `WAIT FOR HOLE CARDS`
  - Usually indicates missing hero hole payloads from the userscript.
  - Check `browser-console.log` for missing `hole` updates.

- Advice does not match current state
  - Compare event sequence in `app-actions.log` and `strategy-training.log`.
  - Confirm hand reset/start events occur before new street updates.

## Development Check

Quick syntax validation:

```bash
python -m py_compile app.py handranker.py
```

## License

No license file is currently included in this repository.

