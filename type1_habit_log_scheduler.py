import os
import sys
import argparse
import datetime
import hashlib
import requests
from dotenv import load_dotenv
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)


def habit_handle(page_id):
    if not page_id:
        return "h:??????"
    h = hashlib.sha1(page_id.encode("utf-8")).hexdigest()[:6]
    return f"h:{h}"

# --- Dry-run / simulated-time globals ----------------------------------------
# Set by parse_cli_args() before main() runs.
#   DRY_RUN: when True, all Notion write operations are short-circuited and
#            logged instead. Reads still happen against real Notion data.
#   SIMULATED_NOW: when set to a tz-aware datetime, now_local() returns this
#                  value instead of the wall clock. Lets us pretend it's
#                  Sunday 3 AM to test the weekly logic without waiting.
DRY_RUN = False
SIMULATED_NOW = None


def now_local():
    """Return current local time, or the simulated time if one is set.

    Use this everywhere instead of datetime.datetime.now(LOCAL_TZ).
    """
    if SIMULATED_NOW is not None:
        return SIMULATED_NOW
    return datetime.datetime.now(LOCAL_TZ)


def dry_run_log(action, **details):
    """Print what would have been written if DRY_RUN is on."""
    detail_str = " ".join(f"{k}={v!r}" for k, v in details.items())
    print(f"[DRY RUN] {action} {detail_str}")
# -----------------------------------------------------------------------------

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
HABITS_DB_ID = os.getenv("HABITS_DB_ID")
HABIT_LOG_DB_ID = os.getenv("HABIT_LOG_DB_ID")
HABIT_CONTROL_DB_ID = os.getenv("HABIT_CONTROL_DB_ID")
HABIT_METRICS_DB_ID = os.getenv("HABIT_METRICS_DB_ID")

NOTION_VERSION = "2022-06-28"
BASE_URL = "https://api.notion.com/v1"

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def query_database(database_id, payload=None):
    url = f"{BASE_URL}/databases/{database_id}/query"
    resp = requests.post(url, headers=HEADERS, json=payload or {})
    resp.raise_for_status()
    return resp.json()


def get_system_paused():
    if not HABIT_CONTROL_DB_ID:
        return False
    payload = {"page_size": 1}
    try:
        data = query_database(HABIT_CONTROL_DB_ID, payload)
    except requests.HTTPError as e:
        print("Warning: could not query Habit System Control DB, treating as not paused.")
        print("Error:", e)
        return False

    results = data.get("results", [])
    if not results:
        return False
    control_page = results[0]
    props = control_page.get("properties", {})
    paused_prop = props.get("Paused?")
    if not paused_prop:
        return False
    return paused_prop.get("checkbox", False)


def query_all_pages(database_id, payload=None):
    url = f"{BASE_URL}/databases/{database_id}/query"
    results = []
    body = dict(payload) if payload else {}
    while True:
        resp = requests.post(url, headers=HEADERS, json=body)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        body["start_cursor"] = data.get("next_cursor")
    return results


def get_local_log_date(page):
    event_prop = page["properties"].get("Event Date")
    if not event_prop:
        return None

    raw = None
    if event_prop.get("formula"):
        f = event_prop["formula"]
        if f.get("type") == "date":
            d = f.get("date") or {}
            raw = d.get("start")
    else:
        d = event_prop.get("date") or {}
        raw = d.get("start")

    if not raw:
        return None

    if len(raw) == 10:
        dt = datetime.datetime.fromisoformat(raw).replace(tzinfo=LOCAL_TZ)
        return dt.date()

    dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ).date()


def is_log_in_period(page, start_date, end_date):
    d = get_local_log_date(page)
    return d is not None and start_date <= d < end_date


def get_local_log_datetime(page):
    event_prop = page["properties"].get("Event Date")
    if not event_prop:
        return None

    raw = None
    if event_prop.get("formula"):
        f = event_prop["formula"]
        if f.get("type") == "date":
            d = f.get("date") or {}
            raw = d.get("start")
    else:
        d = event_prop.get("date") or {}
        raw = d.get("start")

    if not raw:
        return None

    if len(raw) == 10:
        return datetime.datetime.fromisoformat(raw).replace(tzinfo=LOCAL_TZ)

    dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def is_log_in_period_dt(page, start_dt, end_dt):
    dt = get_local_log_datetime(page)
    return dt is not None and start_dt <= dt < end_dt


def get_week_window_sunday_3am(reference_dt=None):
    ref = reference_dt or now_local()
    today = ref.date()
    days_since_sunday = (today.weekday() + 1) % 7
    sunday_date = today - datetime.timedelta(days=days_since_sunday)

    start_dt = datetime.datetime.combine(sunday_date, datetime.time(3, 0), tzinfo=LOCAL_TZ)

    if ref < start_dt:
        start_dt -= datetime.timedelta(days=7)

    end_dt = start_dt + datetime.timedelta(days=7)
    return start_dt, end_dt


def create_page_in_db(database_id, properties, children=None):
    if DRY_RUN:
        title_summary = _extract_title_summary(properties)
        dry_run_log("CREATE_PAGE", database_id=database_id, title=title_summary)
        # Return a fake response shape so callers that read .get('id') don't crash.
        return {"id": "dry-run-fake-id", "properties": properties}
    url = f"{BASE_URL}/pages"
    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }
    if children:
        payload["children"] = children
    resp = requests.post(url, headers=HEADERS, json=payload)
    if not resp.ok:
        print("Create page failed:")
        print("Status:", resp.status_code)
        print("Response:", resp.text)
        print("Payload sent:", payload)
        resp.raise_for_status()
    return resp.json()


def update_page_properties(page_id, properties):
    if DRY_RUN:
        # Show just the numeric / checkbox updates for readability; titles rarely matter on updates.
        summary = {k: v for k, v in properties.items() if k != "Name"}
        dry_run_log("UPDATE_PAGE", page_id=page_id, props=summary)
        return {"id": page_id, "properties": properties}
    url = f"{BASE_URL}/pages/{page_id}"
    payload = {"properties": properties}
    resp = requests.patch(url, headers=HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()


def archive_page(page_id):
    if DRY_RUN:
        dry_run_log("ARCHIVE_PAGE", page_id=page_id)
        return {"id": page_id, "archived": True}
    url = f"{BASE_URL}/pages/{page_id}"
    payload = {"archived": True}
    resp = requests.patch(url, headers=HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()


def _extract_title_summary(properties):
    """Best-effort: pull the page's intended title out of a properties payload for dry-run logs."""
    name_prop = properties.get("Name") or properties.get("Title")
    if not name_prop:
        return "<no title>"
    title_arr = name_prop.get("title", [])
    if title_arr and isinstance(title_arr, list):
        return "".join(t.get("text", {}).get("content", "") for t in title_arr)
    return "<no title>"


def get_today_date_str():
    return now_local().date().isoformat()


def get_today_weekday_name():
    return now_local().strftime("%A")


def get_title_from_property(prop):
    title_array = prop.get("title", [])
    if not title_array:
        return ""
    return title_array[0].get("plain_text", "")


def get_start_and_end_of_week_sunday():
    today = now_local().date()
    days_since_sunday = (today.weekday() + 1) % 7
    start_of_week = today - datetime.timedelta(days=days_since_sunday)
    end_of_week = start_of_week + datetime.timedelta(days=7)
    return start_of_week, end_of_week


def get_week_start_sunday_for_date(d):
    days_since_sunday = (d.weekday() + 1) % 7
    return d - datetime.timedelta(days=days_since_sunday)


def get_habit_types(habit_page):
    props = habit_page.get("properties", {})
    ht = props.get("Habit Type", {})
    return [t.get("name") for t in ht.get("multi_select", [])]


def fetch_log_pages_for_habit_completed_status(habit_page_id, completed_value):
    payload = {
        "filter": {
            "and": [
                {"property": "Habit", "relation": {"contains": habit_page_id}},
                {"property": "Completed?", "checkbox": {"equals": completed_value}},
            ]
        },
        "page_size": 100
    }
    return query_all_pages(HABIT_LOG_DB_ID, payload)


def fetch_log_pages_for_habit_in_period(habit_page_id, start_date, end_date, completed_value):
    pages = fetch_log_pages_for_habit_completed_status(habit_page_id, completed_value)
    return [p for p in pages if is_log_in_period(p, start_date, end_date)]


def fetch_log_pages_for_habit_in_period_dt(habit_page_id, start_dt, end_dt, completed_value):
    pages = fetch_log_pages_for_habit_completed_status(habit_page_id, completed_value)
    return [p for p in pages if is_log_in_period_dt(p, start_dt, end_dt)]


def count_completed_logs_for_habit_in_period(habit_page_id, start_date, end_date):
    return len(fetch_log_pages_for_habit_in_period(habit_page_id, start_date, end_date, True))


def count_all_logs_for_habit_in_period(habit_page_id, start_date, end_date):
    payload = {
        "filter": {"property": "Habit", "relation": {"contains": habit_page_id}},
        "page_size": 100
    }
    pages = query_all_pages(HABIT_LOG_DB_ID, payload)
    return len([p for p in pages if is_log_in_period(p, start_date, end_date)])


def fetch_type1_habits_for_today():
    today_weekday = get_today_weekday_name()
    payload = {
        "filter": {
            "and": [
                {"property": "Active", "checkbox": {"equals": True}},
                {"property": "Habit Type", "multi_select": {"contains": "Type 1"}},
                {"property": "Days of the Week", "multi_select": {"contains": today_weekday}},
            ]
        }
    }
    data = query_database(HABITS_DB_ID, payload)
    return data.get("results", [])


def log_entry_exists_for_habit_today(habit_page_id, log_name):
    payload = {
        "filter": {
            "and": [
                {"property": "Habit", "relation": {"contains": habit_page_id}},
                {"property": "Name", "title": {"equals": log_name}},
            ]
        }
    }
    data = query_database(HABIT_LOG_DB_ID, payload)
    return len(data.get("results", [])) > 0


def create_log_for_type1_habit(habit_page):
    habit_id = habit_page["id"]
    props = habit_page["properties"]
    habit_name = get_title_from_property(props["Habit"])
    today_str = get_today_date_str()
    log_name = f"{today_str} – {habit_name}"

    if log_entry_exists_for_habit_today(habit_id, log_name):
        return None

    properties = {
        "Name": {"title": [{"text": {"content": log_name}}]},
        "Habit": {"relation": [{"id": habit_id}]},
        "Completed?": {"checkbox": False},
        # Explicitly stamp Effective Date so the Event Date formula does NOT
        # fall back to Log Date (the creation timestamp). This makes the log's
        # logical day independent of WHEN the script actually runs — critical
        # for correct missed-instance and streak calculations.
        "Effective Date": {"date": {"start": today_str}},
    }

    page = create_page_in_db(HABIT_LOG_DB_ID, properties)
    return page


def run_type1_log_generation():
    habits = fetch_type1_habits_for_today()
    print("Type 1 – habits matching filters today:", len(habits))
    created_pages = []
    for habit_page in habits:
        page = create_log_for_type1_habit(habit_page)
        if page is not None:
            created_pages.append(page["id"])
    if created_pages:
        print("Type 1 – created log pages:", created_pages)
    else:
        print("Type 1 – no new log pages created")


def create_logs_for_type3_habit(habit_page, week_start, count_to_create):
    habit_id = habit_page["id"]
    props = habit_page["properties"]
    habit_name = get_title_from_property(props["Habit"])

    week_start_iso = week_start.isoformat()
    created_ids = []

    for i in range(int(count_to_create)):
        log_name = f"{habit_name} – Week of {week_start_iso} – #{i + 1}"
        properties = {
            "Name": {"title": [{"text": {"content": log_name}}]},
            "Habit": {"relation": [{"id": habit_id}]},
            "Completed?": {"checkbox": False},
            # Stamp the week-start date so the Event Date formula anchors these
            # logs to the correct week regardless of when they were created.
            "Effective Date": {"date": {"start": week_start_iso}},
        }
        page = create_page_in_db(HABIT_LOG_DB_ID, properties)
        created_ids.append(page["id"])

    return created_ids


def is_type3_week_log(page, habit_name, week_start):
    title_prop = page["properties"].get("Name", {}).get("title", [])
    if not title_prop:
        return False

    title = title_prop[0].get("plain_text", "")
    week_str = week_start.isoformat()

    return title.startswith(f"{habit_name} – Week of {week_str} – #")


def ensure_next_week_type3_logs(habit_page, this_week_start_dt, this_week_end_dt, weekly_target):
    create_logs_for_type3_habit(habit_page, this_week_start_dt.date(), int(weekly_target))
    return []


def fetch_active_habits_for_streaks():
    payload = {"filter": {"property": "Active", "checkbox": {"equals": True}}}
    data = query_all_pages(HABITS_DB_ID, payload)
    return data

def count_logs_for_habit_in_week(habit_page_id, week_start, week_end, completed_value):
    """Count logs for a habit in [week_start, week_end), using local-time semantics.

    week_start and week_end may be either datetime.date or datetime.datetime.
    The previous implementation used Notion's API-side date filter, which treats
    bare dates as midnight UTC -- causing Saturday-evening-local logs to count as
    Sunday's. This version fetches all matching logs (filtered only by Habit
    relation + Completed?) and applies the date check in Python via
    get_local_log_date(), which is timezone-aware.

    Performance: a single habit typically has at most a few hundred logs total,
    so this is fine. If history grows large we can re-add a wide API-side date
    filter (e.g. week_start - 2 days through week_end + 2 days) to narrow the
    fetch while still letting Python do the precise boundary check.
    """
    # Normalize to date objects for comparison
    if isinstance(week_start, datetime.datetime):
        week_start_date = week_start.astimezone(LOCAL_TZ).date()
    else:
        week_start_date = week_start
    if isinstance(week_end, datetime.datetime):
        week_end_date = week_end.astimezone(LOCAL_TZ).date()
    else:
        week_end_date = week_end

    payload = {
        "filter": {
            "and": [
                {"property": "Habit", "relation": {"contains": habit_page_id}},
                {"property": "Completed?", "checkbox": {"equals": completed_value}},
            ]
        },
        "page_size": 100
    }
    pages = query_all_pages(HABIT_LOG_DB_ID, payload)

    count = 0
    for page in pages:
        local_date = get_local_log_date(page)
        if local_date is None:
            continue
        # Inclusive of week_start_date, exclusive of week_end_date — same as the
        # old half-open interval [week_start, week_end).
        if week_start_date <= local_date < week_end_date:
            count += 1
    return count

def weekday_name_to_sunday_index(name):
    mapping = {
        "Sunday": 0,
        "Monday": 1,
        "Tuesday": 2,
        "Wednesday": 3,
        "Thursday": 4,
        "Friday": 5,
        "Saturday": 6,
    }
    return mapping.get(name)


def run_weekly_done_left_goal_update():
    # Use the canonical 3-AM-Sunday window so this matches every other weekly
    # calculation in the script. Previously this used get_start_and_end_of_week_sunday(),
    # which returned bare dates with no 3 AM logic, causing edge-case mismatches.
    week_start_dt, week_end_dt = get_week_window_sunday_3am(now_local())
    week_start = week_start_dt.date()
    week_end = week_end_dt.date()

    habits = fetch_active_habits_for_streaks()
    print("Weekly Done/Left/Goal/Should – active habits:", len(habits))
    print(f"Weekly Done/Left/Goal/Should – week window: {week_start} → {week_end}")

    today_name = now_local().strftime("%A")
    today_idx = weekday_name_to_sunday_index(today_name)

    for habit_page in habits:
        habit_id = habit_page["id"]
        props = habit_page.get("properties", {})

        weekly_target_prop = props.get("Weekly Target")
        weekly_target = weekly_target_prop.get("number") if weekly_target_prop else 0
        weekly_target = weekly_target or 0

        completed_this_week = count_logs_for_habit_in_week(habit_id, week_start, week_end, True)
        uncompleted_this_week = count_logs_for_habit_in_week(habit_id, week_start, week_end, False)

        done_this_week = completed_this_week
        left_this_week = max(0, uncompleted_this_week - completed_this_week)

        days_prop = props.get("Days of the Week", {})
        scheduled_names = [d.get("name") for d in days_prop.get("multi_select", [])]

        should_done = 0
        if today_idx is not None and scheduled_names:
            for n in scheduled_names:
                idx = weekday_name_to_sunday_index(n)
                if idx is not None and idx <= today_idx:
                    should_done += 1

        update_page_properties(habit_id, {
            "Done This Week (Auto)": {"number": done_this_week},
            "Left This Week (Auto)": {"number": left_this_week},
            "Weekly Goal (Auto)": {"number": weekly_target},
            "Should've Done (Auto)": {"number": should_done},
        })

    print("Weekly Done/Left/Goal/Should – update complete")




def get_completed_log_dates_for_habit(habit_page_id, end_date):
    payload = {
        "filter": {
            "and": [
                {"property": "Habit", "relation": {"contains": habit_page_id}},
                {"property": "Completed?", "checkbox": {"equals": True}},
            ]
        },
        "page_size": 100
    }
    results = query_all_pages(HABIT_LOG_DB_ID, payload)
    dates = set()
    for page in results:
        local_date = get_local_log_date(page)
        if local_date and local_date <= end_date:
            dates.add(local_date)
    return dates


def compute_streak_from_dates(completion_dates, end_date):
    streak = 0
    current = end_date
    freezes_used = 0
    max_freezes = 1

    while True:
        if current in completion_dates:
            streak += 1
        else:
            if freezes_used < max_freezes:
                freezes_used += 1
            else:
                break
        current = current - datetime.timedelta(days=1)

    return streak


def compute_type1_streak(completion_dates, end_date, scheduled_weekdays):
    if not scheduled_weekdays:
        return compute_streak_from_dates(completion_dates, end_date)
    if not completion_dates:
        return 0

    streak = 0
    current = end_date
    freezes_used = 0
    max_freezes = 1
    one_day = datetime.timedelta(days=1)

    while True:
        weekday_name = current.strftime("%A")
        if weekday_name not in scheduled_weekdays:
            current -= one_day
            continue

        if current in completion_dates:
            streak += 1
        else:
            if freezes_used < max_freezes:
                freezes_used += 1
            else:
                break

        current -= one_day

    return streak


def compute_best_streak_from_dates(completion_dates):
    if not completion_dates:
        return 0
    sorted_dates = sorted(completion_dates)
    best = 1
    current_run = 1
    prev = sorted_dates[0]
    for d in sorted_dates[1:]:
        if (d - prev).days == 1:
            current_run += 1
        else:
            if current_run > best:
                best = current_run
            current_run = 1
        prev = d
    if current_run > best:
        best = current_run
    return best


def get_type2_log_status_and_earliest_date(habit_page_id, end_date):
    payload = {
        "filter": {
            "property": "Habit",
            "relation": {"contains": habit_page_id},
        },
        "page_size": 100
    }
    results = query_all_pages(HABIT_LOG_DB_ID, payload)
    status_by_date = {}
    earliest_date = None

    for page in results:
        d = get_local_log_date(page)
        if d is None or d > end_date:
            continue

        props = page.get("properties", {})
        completed_prop = props.get("Completed?")
        completed = completed_prop.get("checkbox") if completed_prop else False

        status = status_by_date.get(d, {
            "has_completed": False,
            "has_uncompleted": False,
            "completed_count": 0,
            "uncompleted_count": 0,
        })
        if completed:
            status["has_completed"] = True
            status["completed_count"] += 1
        else:
            status["has_uncompleted"] = True
            status["uncompleted_count"] += 1
        status_by_date[d] = status

        if earliest_date is None or d < earliest_date:
            earliest_date = d

    return status_by_date, earliest_date


def get_earliest_log_date_for_habit(habit_page_id, end_date):
    payload = {
        "filter": {
            "property": "Habit",
            "relation": {"contains": habit_page_id},
        },
        "page_size": 100
    }
    results = query_all_pages(HABIT_LOG_DB_ID, payload)
    earliest_date = None

    for page in results:
        d = get_local_log_date(page)
        if d is None or d > end_date:
            continue
        if earliest_date is None or d < earliest_date:
            earliest_date = d

    return earliest_date


def compute_type2_streaks(status_by_date, earliest_date, end_date):
    if earliest_date is None:
        return 0, 0

    failure_dates = []
    for d, status in status_by_date.items():
        if status["has_uncompleted"] and not status["has_completed"] and d <= end_date:
            failure_dates.append(d)

    if failure_dates:
        last_failure = max(failure_dates)
        current_streak = (end_date - last_failure).days
    else:
        current_streak = (end_date - earliest_date).days + 1

    if not failure_dates:
        best_streak = current_streak
    else:
        failure_dates_sorted = sorted(failure_dates)
        best_streak = 0

        first_failure = failure_dates_sorted[0]
        gap_start_len = (first_failure - earliest_date).days
        if gap_start_len > best_streak:
            best_streak = gap_start_len

        for i in range(len(failure_dates_sorted) - 1):
            prev_failure = failure_dates_sorted[i]
            next_failure = failure_dates_sorted[i + 1]
            gap_len = (next_failure - prev_failure).days - 1
            if gap_len > best_streak:
                best_streak = gap_len

        last_failure = failure_dates_sorted[-1]
        gap_end_len = (end_date - last_failure).days
        if gap_end_len > best_streak:
            best_streak = gap_end_len

    return current_streak, best_streak


def compute_type3_week_streaks(habit_page, end_date):
    props = habit_page.get("properties", {})
    weekly_target_prop = props.get("Weekly Target")
    weekly_target = weekly_target_prop.get("number") if weekly_target_prop else None

    if not weekly_target or weekly_target <= 0:
        return 0, 0

    this_week_start, _ = get_start_and_end_of_week_sunday()
    last_week_end = this_week_start
    last_week_start = this_week_start - datetime.timedelta(days=7)

    earliest_date = get_earliest_log_date_for_habit(habit_page["id"], end_date)
    if earliest_date is None:
        return 0, 0

    first_week_start = get_week_start_sunday_for_date(earliest_date)
    if first_week_start > last_week_start:
        return 0, 0

    success_by_week = {}
    ws = first_week_start
    while ws <= last_week_start:
        we = ws + datetime.timedelta(days=7)
        completed_count = count_completed_logs_for_habit_in_period(habit_page["id"], ws, we)
        success_by_week[ws] = (completed_count >= weekly_target)
        ws = ws + datetime.timedelta(days=7)

    current_streak = 0
    ws = last_week_start
    while ws in success_by_week and success_by_week[ws]:
        current_streak += 1
        ws = ws - datetime.timedelta(days=7)

    best_streak = 0
    run = 0
    for ws in sorted(success_by_week.keys()):
        if success_by_week[ws]:
            run += 1
            if run > best_streak:
                best_streak = run
        else:
            run = 0

    return current_streak, best_streak


# -----------------------------------------------------------------------------
# "Never miss twice" tracking
# -----------------------------------------------------------------------------
# Two checkbox properties on Habits (Unified) drive the Morning Brief view:
#   Missed Last Instance (Auto)       — the most recent instance was missed
#   Missed Multiple in a Row (Auto)   — two or more in a row missed
#
# Both can be true simultaneously. The Morning Brief surfaces them in two
# separate sections so "missed once, get back on the wagon" stays visually
# distinct from "missed multiple, on the wagon-falling-off track".
#
# Per-habit-type rules:
#   Type 1: "instance" = each scheduled weekday strictly before today
#   Type 2: "instance" = each day strictly before today
#   Type 3: "instance" = each completed weekly cycle ending before this week.
#           Evaluated against weekly_target.
# -----------------------------------------------------------------------------


def _type1_scheduled_dates_descending(scheduled_weekday_names, end_date, max_count):
    """Yield scheduled weekday dates walking backwards from end_date (inclusive).

    If no weekdays are specified, treat every day as scheduled (same fallback
    compute_type1_streak uses).
    """
    if not scheduled_weekday_names:
        scheduled_weekday_names = {
            "Sunday", "Monday", "Tuesday", "Wednesday",
            "Thursday", "Friday", "Saturday",
        }
    current = end_date
    found = 0
    # Hard cap on lookback to avoid runaway loops on weirdly-configured habits.
    safety_limit = 365
    steps = 0
    while found < max_count and steps < safety_limit:
        if current.strftime("%A") in scheduled_weekday_names:
            yield current
            found += 1
        current -= datetime.timedelta(days=1)
        steps += 1


def _was_instance_missed(status_by_date, date):
    """An instance counts as 'missed' when, for that date, uncompleted logs
    outnumber completed logs.

    This matches the ledger model: each scheduled instance creates an
    uncompleted log, and completing it adds a paired completed log. So a day
    where you did everything has equal completed/uncompleted counts; a day
    with an unmatched uncompleted log is a real miss.

    A date with NO logs at all is treated as 'no instance' — not missed.
    (Falls back gracefully if counts are absent, e.g. from older cached data.)
    """
    status = status_by_date.get(date)
    if not status:
        return False

    # Prefer count-based comparison (ledger-aware).
    if "uncompleted_count" in status and "completed_count" in status:
        return status["uncompleted_count"] > status["completed_count"]

    # Fallback to boolean flags if counts somehow missing.
    if status.get("has_completed"):
        return False
    return status.get("has_uncompleted", False)


def _has_any_instance(status_by_date, date):
    """True if any log exists for this date, regardless of completion."""
    status = status_by_date.get(date)
    if not status:
        return False
    return status.get("has_completed", False) or status.get("has_uncompleted", False)


def compute_type1_missed_flags(habit_page, status_by_date, today):
    """Returns (missed_last, missed_multiple) for a Type 1 habit."""
    props = habit_page.get("properties", {})
    days_prop = props.get("Days of the Week", {})
    scheduled_names = {d.get("name") for d in days_prop.get("multi_select", [])}

    # Look back at the two most recent scheduled days strictly before today.
    yesterday = today - datetime.timedelta(days=1)
    prior_dates = list(_type1_scheduled_dates_descending(scheduled_names, yesterday, max_count=2))
    if not prior_dates:
        return False, False

    last_instance = prior_dates[0]
    missed_last = _was_instance_missed(status_by_date, last_instance)

    if len(prior_dates) < 2 or not missed_last:
        return missed_last, False

    second_to_last_instance = prior_dates[1]
    missed_second = _was_instance_missed(status_by_date, second_to_last_instance)
    return missed_last, (missed_last and missed_second)


def compute_type2_missed_flags(status_by_date, today):
    """Returns (missed_last, missed_multiple) for a Type 2 habit."""
    yesterday = today - datetime.timedelta(days=1)
    day_before = today - datetime.timedelta(days=2)

    missed_last = _was_instance_missed(status_by_date, yesterday)
    if not missed_last:
        return False, False

    missed_second = _was_instance_missed(status_by_date, day_before)
    return True, missed_second


def compute_type3_missed_flags(habit_page, today):
    """Returns (missed_last, missed_multiple) for a Type 3 habit.

    'Prior instance' = the week that just ended (the last full Sunday-3am to
    Sunday-3am cycle). We only flag based on completed weeks, never mid-week,
    per the user's design choice.
    """
    props = habit_page.get("properties", {})
    weekly_target_prop = props.get("Weekly Target")
    weekly_target = weekly_target_prop.get("number") if weekly_target_prop else 0
    weekly_target = weekly_target or 0
    if weekly_target <= 0:
        return False, False

    # The current week window (the one we're inside right now).
    this_week_start_dt, _ = get_week_window_sunday_3am(now_local())
    this_week_start_date = this_week_start_dt.date()

    # Last week = the week ending at this_week_start_date (exclusive).
    last_week_end_date = this_week_start_date
    last_week_start_date = last_week_end_date - datetime.timedelta(days=7)

    # Week-before-last for the "missed multiple" check.
    prior_week_end_date = last_week_start_date
    prior_week_start_date = prior_week_end_date - datetime.timedelta(days=7)

    habit_id = habit_page["id"]
    last_week_completed = count_logs_for_habit_in_week(
        habit_id, last_week_start_date, last_week_end_date, True
    )
    missed_last = last_week_completed < weekly_target

    if not missed_last:
        return False, False

    prior_week_completed = count_logs_for_habit_in_week(
        habit_id, prior_week_start_date, prior_week_end_date, True
    )
    missed_second = prior_week_completed < weekly_target

    return True, missed_second


def run_missed_last_instance_update():
    """Compute and write 'Missed Last Instance (Auto)' and 'Missed Multiple in
    a Row (Auto)' for every active habit.

    Type-aware logic — see compute_*_missed_flags helpers above.
    """
    today = now_local().date()
    habits = fetch_active_habits_for_streaks()
    print("Missed-last-instance – active habits:", len(habits))

    for habit_page in habits:
        habit_id = habit_page["id"]
        types = set(get_habit_types(habit_page))

        # We accumulate flags across all assigned types. The OR semantics let a
        # habit tagged as both Type 1 and Type 3 be flagged by either rule —
        # matches the existing streak code's per-type OR behavior.
        missed_last_any = False
        missed_multiple_any = False

        # Type 1 & Type 2 both need a daily status map.
        needs_daily_status = ("Type 1" in types) or ("Type 2" in types)
        status_by_date = None
        if needs_daily_status:
            # We need ~14 days of lookback at most; query end_date is yesterday.
            yesterday = today - datetime.timedelta(days=1)
            status_by_date, _ = get_type2_log_status_and_earliest_date(habit_id, yesterday)

        if "Type 1" in types:
            ml, mm = compute_type1_missed_flags(habit_page, status_by_date or {}, today)
            missed_last_any = missed_last_any or ml
            missed_multiple_any = missed_multiple_any or mm

        if "Type 2" in types:
            ml, mm = compute_type2_missed_flags(status_by_date or {}, today)
            missed_last_any = missed_last_any or ml
            missed_multiple_any = missed_multiple_any or mm

        if "Type 3" in types:
            ml, mm = compute_type3_missed_flags(habit_page, today)
            missed_last_any = missed_last_any or ml
            missed_multiple_any = missed_multiple_any or mm

        if missed_multiple_any:
            marker = "⚠️  missed multiple"
        elif missed_last_any:
            marker = "🚨 missed last"
        else:
            marker = "✓  on track"
        print(f"  {marker:<22} {habit_handle(habit_id)}")

        update_page_properties(habit_id, {
            "Missed Last Instance (Auto)": {"checkbox": missed_last_any},
            "Missed Multiple in a Row (Auto)": {"checkbox": missed_multiple_any},
        })

    print("Missed-last-instance – update complete")


def run_streaks_update():
    today = now_local().date()
    end_date = today - datetime.timedelta(days=1)
    habits = fetch_active_habits_for_streaks()
    print("Streaks – active habits:", len(habits))

    for habit_page in habits:
        habit_id = habit_page["id"]
        props = habit_page.get("properties", {})
        habit_types = get_habit_types(habit_page)

        is_type1 = "Type 1" in habit_types
        is_type2 = "Type 2" in habit_types
        is_type3 = "Type 3" in habit_types

        if is_type3:
            current_streak, best_streak = compute_type3_week_streaks(habit_page, end_date)
        elif is_type2:
            status_by_date, earliest_date = get_type2_log_status_and_earliest_date(habit_id, end_date)
            current_streak, best_streak = compute_type2_streaks(status_by_date, earliest_date, end_date)
        else:
            completion_dates = get_completed_log_dates_for_habit(habit_id, end_date)

            if is_type1:
                days_prop = props.get("Days of the Week", {})
                scheduled_weekdays = {d.get("name") for d in days_prop.get("multi_select", [])}
                current_streak = compute_type1_streak(completion_dates, end_date, scheduled_weekdays)
            else:
                current_streak = compute_streak_from_dates(completion_dates, end_date)

            best_streak = compute_best_streak_from_dates(completion_dates)

        props_update = {
            "Auto Streak": {"number": current_streak},
            "Auto Best Streak": {"number": best_streak},
        }
        update_page_properties(habit_id, props_update)

    print("Streaks – update complete")


def run_weekly_completion_update():
    week_start, week_end = get_start_and_end_of_week_sunday()
    habits = fetch_active_habits_for_streaks()
    print("Weekly Completion – active habits:", len(habits))

    for habit_page in habits:
        habit_id = habit_page["id"]
        props = habit_page.get("properties", {})

        weekly_target_prop = props.get("Weekly Target")
        weekly_target = weekly_target_prop.get("number") if weekly_target_prop else None

        if not weekly_target or weekly_target <= 0:
            weekly_completion = 0
        else:
            completed_count = count_completed_logs_for_habit_in_period(habit_id, week_start, week_end)
            weekly_completion = min(1, round(completed_count / weekly_target, 2))

        update_page_properties(habit_id, {
            "Weekly Completion (Auto)": {"number": weekly_completion}
        })

    print("Weekly Completion – update complete")


def create_habit_metric_entry(habit_page, period_type, start_date, end_date, completed_count, target_count):
    if not HABIT_METRICS_DB_ID:
        return None

    habit_id = habit_page["id"]
    props = habit_page.get("properties", {})
    habit_name = get_title_from_property(props["Habit"])

    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()

    completion_pct = 0
    if target_count and target_count > 0:
        completion_pct = round(min(1, completed_count / target_count), 2)

    auto_streak_prop = props.get("Auto Streak")
    auto_best_prop = props.get("Auto Best Streak")
    snapshot_streak = auto_streak_prop.get("number") if auto_streak_prop else None
    snapshot_best = auto_best_prop.get("number") if auto_best_prop else None

    if period_type == "Week":
        name = f"{habit_name} – Week of {start_iso}"
    else:
        name = f"{habit_name} – Month starting {start_iso}"

    properties = {
        "Name": {"title": [{"text": {"content": name}}]},
        "Habit": {"relation": [{"id": habit_id}]},
        "Period Type": {"select": {"name": period_type}},
        "Period Start": {"date": {"start": start_iso}},
        "Period End": {"date": {"start": end_iso}},
        "Completed Count": {"number": completed_count},
        "Target Count": {"number": target_count if target_count is not None else 0},
        "Completion %": {"number": completion_pct},
        "Snapshot Streak": {"number": snapshot_streak if snapshot_streak is not None else 0},
        "Snapshot Best Streak": {"number": snapshot_best if snapshot_best is not None else 0},
    }

    return create_page_in_db(HABIT_METRICS_DB_ID, properties)


def run_weekly_metrics_snapshot():
    current_now = now_local()
    if current_now.weekday() != 6:
        print("Weekly metrics – today is not Sunday, skipping snapshot")
        return

    this_week_start_dt, this_week_end_dt = get_week_window_sunday_3am(current_now)
    if current_now < this_week_start_dt:
        print("Weekly metrics – before 3 AM boundary, skipping snapshot")
        return

    prev_week_end_dt = this_week_start_dt
    prev_week_start_dt = this_week_start_dt - datetime.timedelta(days=7)

    week_start = prev_week_start_dt.date()
    week_end = prev_week_end_dt.date()

    habits = fetch_active_habits_for_streaks()
    print("Weekly metrics – active habits:", len(habits))

    for habit_page in habits:
        habit_id = habit_page["id"]
        props = habit_page.get("properties", {})
        habit_types = get_habit_types(habit_page)

        weekly_target_prop = props.get("Weekly Target")
        weekly_target = weekly_target_prop.get("number") if weekly_target_prop else None
        target_count = weekly_target if weekly_target and weekly_target > 0 else None

        completed_pages_prev_week = fetch_log_pages_for_habit_in_period_dt(
            habit_id, prev_week_start_dt, prev_week_end_dt, True
        )
        completed_count = len(completed_pages_prev_week)

        create_habit_metric_entry(
            habit_page,
            period_type="Week",
            start_date=week_start,
            end_date=week_end,
            completed_count=completed_count,
            target_count=target_count
        )

        if "Type 3" in habit_types and target_count:
            ensure_next_week_type3_logs(
                habit_page, this_week_start_dt, this_week_end_dt, target_count
            )
    print("Weekly metrics – snapshot complete")
    

def get_previous_month_start_and_end(date):
    first_of_this_month = date.replace(day=1)
    last_day_previous_month = first_of_this_month - datetime.timedelta(days=1)
    start_prev_month = last_day_previous_month.replace(day=1)
    end_prev_month = first_of_this_month
    return start_prev_month, end_prev_month


def run_monthly_metrics_snapshot():
    today = now_local().date()
    if today.day != 1:
        print("Monthly metrics – today is not the first, skipping snapshot")
        return

    month_start, month_end = get_previous_month_start_and_end(today)
    habits = fetch_active_habits_for_streaks()
    print("Monthly metrics – active habits:", len(habits))

    days_in_range = (month_end - month_start).days

    for habit_page in habits:
        props = habit_page.get("properties", {})
        weekly_target_prop = props.get("Weekly Target")
        weekly_target = weekly_target_prop.get("number") if weekly_target_prop else None

        completed_count = count_completed_logs_for_habit_in_period(habit_page["id"], month_start, month_end)

        target_count = None
        if weekly_target and weekly_target > 0:
            target_count = round(weekly_target * (days_in_range / 7))

        create_habit_metric_entry(
            habit_page,
            period_type="Month",
            start_date=month_start,
            end_date=month_end,
            completed_count=completed_count,
            target_count=target_count
        )

    print("Monthly metrics – snapshot complete")


def set_last_run_timestamp():
    if not HABIT_CONTROL_DB_ID:
        return
    payload = {"page_size": 1}
    try:
        data = query_database(HABIT_CONTROL_DB_ID, payload)
    except requests.HTTPError as e:
        print("Warning: could not update Last Run (Auto); control DB query failed.")
        print("Error:", e)
        return

    results = data.get("results", [])
    if not results:
        print("Warning: Habit System Control DB has no pages; cannot set Last Run (Auto).")
        return

    control_page_id = results[0]["id"]
    # Honor SIMULATED_NOW when set, so dry-runs of Sunday 3 AM produce a
    # consistent timestamp; otherwise use real UTC wall clock.
    if SIMULATED_NOW is not None:
        now_utc = SIMULATED_NOW.astimezone(datetime.timezone.utc).isoformat()
    else:
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

    props = {"Last Run (Auto)": {"date": {"start": now_utc}}}

    try:
        update_page_properties(control_page_id, props)
        print("Last Run (Auto) updated to", now_utc)
    except requests.HTTPError as e:
        print("Warning: failed to update Last Run (Auto).")
        print("Error:", e)


def parse_cli_args():
    """Parse command-line args for the optional dry-run / simulate-now modes."""
    parser = argparse.ArgumentParser(
        description="Habit log scheduler. By default runs against real Notion data on the real clock."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read from Notion but don't write. All create/update/archive calls are logged instead.",
    )
    parser.add_argument(
        "--simulate-now",
        type=str,
        default=None,
        help=(
            "Pretend the current time is this ISO-8601 datetime (e.g. '2026-05-24T03:05:00-04:00'). "
            "Useful for testing Sunday/month-boundary logic without waiting. "
            "Strongly recommended to combine with --dry-run."
        ),
    )
    return parser.parse_args()


def main():
    global DRY_RUN, SIMULATED_NOW
    args = parse_cli_args()
    DRY_RUN = args.dry_run
    if args.simulate_now:
        SIMULATED_NOW = datetime.datetime.fromisoformat(args.simulate_now)
        if SIMULATED_NOW.tzinfo is None:
            SIMULATED_NOW = SIMULATED_NOW.replace(tzinfo=LOCAL_TZ)

    if DRY_RUN:
        print("=== DRY RUN MODE — no writes will be made to Notion ===")
    if SIMULATED_NOW is not None:
        print(f"=== SIMULATED TIME: {SIMULATED_NOW.isoformat()} ===")

    print("NOTION_TOKEN set:", NOTION_TOKEN is not None)
    print("HABITS_DB_ID:", HABITS_DB_ID)
    print("HABIT_LOG_DB_ID:", HABIT_LOG_DB_ID)
    print("HABIT_CONTROL_DB_ID:", HABIT_CONTROL_DB_ID)
    print("HABIT_METRICS_DB_ID:", HABIT_METRICS_DB_ID)

    if get_system_paused():
        print("System is paused – skipping log generation, streaks, weekly completion, and metrics snapshots")
        return

    # Run order matters:
    #   1. Generate today's Type 1 logs (if any are scheduled).
    #   2. Run the weekly metrics snapshot. On Sundays this also pre-creates
    #      next week's Type 3 logs, which step 3 needs to count correctly.
    #   3. Update Done / Left / Goal / Should've Done — now that next week's
    #      Type 3 logs exist, "Left This Week" will be right on Sunday morning.
    #   4. Recompute streaks.
    #   5. Compute "Missed Last Instance" / "Missed Multiple in a Row" flags.
    #      Runs after Type 3 logs for the new week exist, so the Type 3 rule
    #      sees the correct "last completed week" boundary.
    #   6. Refresh Weekly Completion %.
    #   7. Monthly metrics snapshot (1st of month only).
    #   8. Heartbeat.
    run_type1_log_generation()
    run_weekly_metrics_snapshot()
    run_weekly_done_left_goal_update()
    run_streaks_update()
    run_missed_last_instance_update()
    run_weekly_completion_update()
    run_monthly_metrics_snapshot()
    set_last_run_timestamp()


if __name__ == "__main__":
    main()
