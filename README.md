# notion-habits-bot

Python script that automates the [Habits (Unified)](https://www.notion.so/2b687b1fd3af8088b6ddc163e142450e) system in Notion.

## What it does

Runs daily and:
- Generates today's Type 1 habit log entries
- Updates Done / Left / Goal / Should've Done counters
- Recomputes streaks (Type 1, 2, and 3)
- Updates Weekly Completion %
- On Sundays after 3 AM local: writes Week metrics snapshots and pre-creates next week's Type 3 logs
- On the 1st of each month: writes Month metrics snapshots
- Stamps `Last Run (Auto)` on the Habit System Control row (the heartbeat the Morning Brief checks)

Honors the `Paused?` checkbox in Habit System Control — if checked, the script does nothing.

## How it runs

**In production:** GitHub Actions runs it daily at 8:00 UTC (3 AM EST / 4 AM EDT). See `.github/workflows/run-habits.yml`. You can also trigger a run manually from the Actions tab.

**Locally:** Copy `.env.example` to `.env`, fill in real values, then:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python type1_habit_log_scheduler.py
```

## Secrets

In GitHub Actions, these are set as repository secrets (Settings → Secrets and variables → Actions):

- `NOTION_TOKEN` — Notion integration token
- `HABITS_DB_ID` — Habits (Unified) database ID
- `HABIT_LOG_DB_ID` — Habit Log database ID
- `HABIT_CONTROL_DB_ID` — Habit System Control database ID
- `HABIT_METRICS_DB_ID` — Habit Metrics database ID

Locally, the same names go in `.env` (which is gitignored).
