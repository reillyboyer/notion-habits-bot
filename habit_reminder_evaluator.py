"""Habit reminder trigger evaluator.

For each active habit in Habits (Unified), computes the three habit-specific
trigger conditions and writes them to checkbox properties:

  Trigger: Today Not Yet Done (Auto)         — today's instance not yet completed
  Trigger: Missed Last Instance (Auto)       — mirrors existing Missed Last Instance flag
  Trigger: Behind on Weekly Target (Auto)    — Type 3 only, Wed+ and done < target/2

The reminder dispatcher reads these checkboxes; it does NOT compute them
itself. This script is the bridge between domain-specific habit logic and
the generic reminder service.

Designed to be called from the main habit script after the rest of the
daily work, but can also run standalone. Intended frequency: once daily,
right after the main habit run (so it sees fresh Missed Last Instance flags).
"""

import os
import sys
import datetime
import requests
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

LOCAL_TZ = ZoneInfo("America/New_York")

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
HABITS_DB_ID = os.environ.get("HABITS_DB_ID")
HABIT_LOG_DB_ID = os.environ.get("HABIT_LOG_DB_ID")

NOTION_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

DRY_RUN = "--dry-run" in sys.argv
SIMULATED_NOW = None
for i, arg in enumerate(sys.argv):
    if arg == "--simulate-now" and i + 1 < len(sys.argv):
        SIMULATED_NOW = datetime.datetime.fromisoformat(sys.argv[i + 1])
        if SIMULATED_NOW.tzinfo is None:
            SIMULATED_NOW = SIMULATED_NOW.replace(tzinfo=LOCAL_TZ)


def now_local():
    return SIMULATED_NOW if SIMULATED_NOW else datetime.datetime.now(LOCAL_TZ)


# === Notion helpers =========================================================

def query_all_pages(database_id, payload):
    results = []
    cursor = None
    while True:
        body = dict(payload)
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(
            f"{NOTION_BASE_URL}/databases/{database_id}/query",
            headers=HEADERS,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def update_page(page_id, properties):
    if DRY_RUN:
        print(f"  [DRY RUN] UPDATE {page_id} {properties}")
        return
    resp = requests.patch(
        f"{NOTION_BASE_URL}/pages/{page_id}",
        headers=HEADERS,
        json={"properties": properties},
    )
    resp.raise_for_status()


def fetch_active_habits():
    return query_all_pages(HABITS_DB_ID, {
        "filter": {"property": "Active", "checkbox": {"equals": True}},
        "page_size": 100,
    })


def get_title(habit):
    arr = habit.get("properties", {}).get("Habit", {}).get("title", [])
    return "".join(t.get("plain_text", "") for t in arr).strip()


def get_habit_types(habit):
    types = habit.get("properties", {}).get("Habit Type", {}).get("multi_select", [])
    return [t.get("name") for t in types if t.get("name")]


def get_checkbox(habit, name):
    return bool(habit.get("properties", {}).get(name, {}).get("checkbox", False))


def get_number(habit, name, default=0):
    v = habit.get("properties", {}).get(name, {}).get("number")
    return v if v is not None else default


# === Today Not Yet Done =====================================================

def today_log_status(habit_id):
    """Returns (uncompleted_count, completed_count) for today's logs.

    Uses the ledger model: a completed instance has 1 uncompleted + 1
    completed log on the same day. Day is determined by the log name prefix
    (which the main script formats as 'YYYY-MM-DD – {habit name}') for Type 1
    logs. For Type 3 logs (named 'Habit – Week of YYYY-MM-DD – #N') we don't
    use this trigger.
    """
    today_str = now_local().date().isoformat()
    payload = {
        "filter": {"property": "Habit", "relation": {"contains": habit_id}},
        "page_size": 100,
    }
    pages = query_all_pages(HABIT_LOG_DB_ID, payload)
    uncompleted = 0
    completed = 0
    for page in pages:
        name_arr = page.get("properties", {}).get("Name", {}).get("title", [])
        name = "".join(t.get("plain_text", "") for t in name_arr)
        if not name.startswith(today_str):
            continue
        completed_prop = page.get("properties", {}).get("Completed?", {})
        if completed_prop.get("checkbox"):
            completed += 1
        else:
            uncompleted += 1
    return uncompleted, completed


def is_today_not_yet_done(habit):
    """True iff there's at least one unmatched uncompleted log for today
    (i.e., an instance was scheduled today but no completed log cancels it).

    Only meaningful for habits that have today's instance scheduled — for
    Type 1 habits on a scheduled weekday. We rely on the main habit script
    having created today's log earlier in the day; if no log exists, this
    returns False (no instance today → not 'not yet done')."""
    uncompleted, completed = today_log_status(habit["id"])
    if uncompleted == 0:
        return False  # no instance scheduled today (or no log made yet)
    return uncompleted > completed


# === Behind on Weekly Target ================================================

def is_behind_on_weekly_target(habit):
    """Type 3 only. Strict rule: today is Wednesday or later AND Done This Week
    < Weekly Target / 2.

    Week starts Sunday (3am boundary handled in the main script; here we just
    use weekday names). Sunday=6 in Python's weekday(), Wednesday=2.
    Our rule: weekday >= 2 (Wednesday in Mon=0 indexing).
    """
    types = get_habit_types(habit)
    if "Type 3" not in types:
        return False
    target = get_number(habit, "Weekly Target")
    if target <= 0:
        return False
    done = get_number(habit, "Done This Week (Auto)")

    # Python: Mon=0, Tue=1, Wed=2, ..., Sun=6.
    # We want "Wednesday or later in a Sunday-starting week", i.e., a day where
    # at least Sun, Mon, Tue have passed. Sun-start day indices: Sun=0, Mon=1,
    # Tue=2, Wed=3. So Wednesday-or-later in Sunday-start indexing is index >= 3.
    py_wd = now_local().weekday()
    sun_start_index = (py_wd + 1) % 7  # Sun=0, Mon=1, Tue=2, Wed=3, ...
    if sun_start_index < 3:
        return False

    return done < (target / 2.0)


# === Main ===================================================================

def evaluate_habit(habit):
    title = get_title(habit)
    today_not_done = is_today_not_yet_done(habit)
    missed_last = get_checkbox(habit, "Missed Last Instance (Auto)")
    behind = is_behind_on_weekly_target(habit)

    update_page(habit["id"], {
        "Trigger: Today Not Yet Done (Auto)": {"checkbox": today_not_done},
        "Trigger: Missed Last Instance (Auto)": {"checkbox": missed_last},
        "Trigger: Behind on Weekly Target (Auto)": {"checkbox": behind},
    })

    flags = []
    if today_not_done: flags.append("today-not-done")
    if missed_last: flags.append("missed-last")
    if behind: flags.append("behind-weekly")
    flags_str = ", ".join(flags) if flags else "no triggers"
    print(f"  {title:<40} {flags_str}")


def main():
    if not all([NOTION_TOKEN, HABITS_DB_ID, HABIT_LOG_DB_ID]):
        print("Missing required env vars.")
        sys.exit(1)

    if DRY_RUN:
        print("=== DRY RUN — no Notion writes ===")
    if SIMULATED_NOW:
        print(f"=== SIMULATED TIME: {SIMULATED_NOW.isoformat()} ===")

    habits = fetch_active_habits()
    print(f"Habit reminder evaluator — {len(habits)} active habits")
    for habit in habits:
        evaluate_habit(habit)
    print("Done.")


if __name__ == "__main__":
    main()
