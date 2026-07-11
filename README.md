# Poker Hand Trainer by Cha0i

A desktop poker training app with live board/hole ingestion, equity simulation, and a practical strategy coach.

This project combines:
- A Tkinter GUI for selecting cards and viewing odds.
- A local browser bridge server that ingests tagged game updates.
- A strategy panel that reacts to street, pot, to-call, and action pressure.
- Structured training logs for analyzing advice versus outcomes.

## Features

- Interactive card grid:
	- Select 2 hole cards and up to 5 board cards.
	- Enforces card uniqueness across hole/board.

- Odds and hand distribution:
	- Win/tie/loss equity vs configurable player count.
	- Final hand-category distribution for hero and a random opponent.

- Smart Play Coach:
	- Street-aware (preflop/flop/turn/river).
	- Uses pot odds, equity, aggression, and board texture.
	- Produces concise action-first headlines plus reasoning.

- Live browser bridge:
	- Built-in local server receives tagged payloads (`TM_BRIDGE:{...}`).
	- Applies hole/board/players/reset updates to the UI.

- Training data and diagnostics:
	- Strategy snapshots, hero actions, reveals, and hand outcomes in JSONL.
	- App-side action logs and captured browser lines for forensics.

## Project Files

- `app.py`: main Tkinter application, strategy engine, bridge ingestion, logging.
- `handranker.py`: preflop evaluation, equity simulation, hand ranking utilities.
- `tampermonkey-bridge.user.js`: userscript bridge that emits tagged payloads.
- `browser-console.log`: captured raw browser console lines.
- `app-actions.log`: detailed app-side ingestion/apply/action trace.
- `strategy-training.log`: compact strategy-training JSONL dataset.

## Requirements

- Python 3.10+
- Packages used by the app:
	- `flask`
	- `werkzeug`

The repository already includes `.venv/` in your current workspace setup.

## Run

From the project root:

```bash
source .venv/bin/activate
python3 app.py
```

## Bridge Setup (Tampermonkey)

1. Install/import `tampermonkey-bridge.user.js` into Tampermonkey.
2. Launch the app.
3. In the app, click `Start Server` in the Browser Bridge panel.
4. Ensure the userscript posts to `http://127.0.0.1:5000/log`.
5. Watch the bridge log panel for accepted tagged payloads.

Expected tagged format:

```json
TM_BRIDGE:{"type":"poker_cards","hole":["As","Kd"],"board":["7c","2d","Th"],"players":4}
```

Supported bridge fields:
- `type`: must be `"poker_cards"`
- `hole`: exactly 2 cards
- `board`: 0 to 5 cards
- `players`: integer (clamped to 2..10)
- `reset`: boolean

## Strategy and Training Logs

`strategy-training.log` stores JSONL events such as:
- `hand_start`
- `advice_snapshot`
- `hole_set`
- `community_reveal`
- `hero_action`
- `hand_end`

This enables post-session analysis of:
- Advice consistency by street and pressure.
- Advice recommendation vs observed outcomes.
- Data quality issues (for example, long `WAIT FOR HOLE CARDS` stretches).

## Troubleshooting

- Bridge shows offline:
	- Verify app server is running (`Start Server`).
	- Confirm no process is blocking `127.0.0.1:5000`.

- Repeated `WAIT FOR HOLE CARDS`:
	- Usually means hero hole cards were not emitted by the userscript for that hand.
	- Check `browser-console.log` for missing `TM_BRIDGE` `hole` payloads.

- Advice feels mismatched to state:
	- Inspect `app-actions.log` and `strategy-training.log` for event order.
	- Confirm hand transitions include reset/start and fresh hole payloads.

- Syntax check after edits:

```bash
.venv/bin/python -m py_compile app.py handranker.py
```

## Notes

- The app writes local logs continuously during use.
- If you share this project publicly, consider excluding large log artifacts.

## License

No license file is currently included in this repository.
