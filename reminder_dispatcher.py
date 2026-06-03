"""Generic reminder dispatcher.

Runs every 5 minutes (via GitHub Actions, cron `*/5 * * * *`). Knows nothing
about habits specifically — instead, it scans configured Notion databases for
pages that have the "Reminder Properties" set, evaluates which triggers are
currently active by reading per-page trigger-state checkboxes, and fires
Pushover notifications with priority-aware escalation.

To extend the system to a new use case (e.g., reminding you about overdue
tasks, project deadlines, etc.), you:
  1. Add the standard Reminder Properties to that database (see SCHEMA below).
  2. Add the new database's ID to REMINDER_TARGET_DB_IDS env var.
  3. Add trigger-state checkboxes to that database (whatever conditions you
     want — the dispatcher reads any `Trigger: X (Auto)` checkbox).
  4. Build a separate evaluator script (or formula) that computes those
     trigger checkboxes for that database's pages.

The dispatcher is intentionally dumb: it does NOT compute trigger conditions,
only reads them. Each domain's evaluator is responsible for setting its own
trigger state. For the habit system, see habit_reminder_evaluator.py.

SCHEMA — properties expected on each reminder-capable page:
  Reminders On                          checkbox    master switch
  Reminder Triggers                     multi-select  which trigger conditions matter
  Reminder Frequency                    select        when to fire
  Custom Time                           rich_text     used iff Frequency = "Custom time"
  Reminder Priority                     select        Normal / High
  Reminder Acknowledged                 checkbox      manual stop for High escalation
  Reminder Last Sent At (Auto)          date          auto-managed; last send timestamp
  Reminder Send Count (Auto)            number        auto-managed; sends in current escalation
  Trigger: <name> (Auto)                checkbox      one per trigger option (set by evaluator)

Active hours are fixed: 08:00 local through 01:00 next-day local.
"""

import os
import re
import sys
import hashlib
import datetime
import requests
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

LOCAL_TZ = ZoneInfo("America/New_York")

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)


def page_handle(page_id):
    """Stable 6-char identifier for a Notion page ID.

    Used in log output instead of page titles so that GitHub Actions logs
    (publicly visible on public repos) don't leak personal content like habit
    names. Deterministic — same page always gets the same handle for
    consistent debugging.
    """
    if not page_id:
        return "p:??????"
    h = hashlib.sha1(page_id.encode("utf-8")).hexdigest()[:6]
    return f"p:{h}"

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
PUSHOVER_USER_KEY = os.environ.get("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.environ.get("PUSHOVER_API_TOKEN")

# Comma-separated list of Notion database IDs the dispatcher should scan.
# For now this is just the habits DB; future databases get appended here.
REMINDER_TARGET_DB_IDS = [
    db_id.strip()
    for db_id in os.environ.get("REMINDER_TARGET_DB_IDS", "").split(",")
    if db_id.strip()
]

# Where notifications deep-link to.
NOTIFICATION_URL = (
    "https://app.notion.com/p/Quick-Habit-Tracker-27a87b1fd3af81418116c9a0ea71806c"
)

NOTION_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

# Active hours: 08:00 → 01:00 next day (i.e., until 01:59:59).
# We allow hour h iff (h >= 8) or (h < 2).
ACTIVE_START_HOUR = 8
ACTIVE_END_HOUR_EXCLUSIVE = 2  # 0:00 and 1:00 still count

# CLI flags ------------------------------------------------------------------
DRY_RUN = "--dry-run" in sys.argv
VERBOSE = "--verbose" in sys.argv
SIMULATED_NOW = None
for i, arg in enumerate(sys.argv):
    if arg == "--simulate-now" and i + 1 < len(sys.argv):
        SIMULATED_NOW = datetime.datetime.fromisoformat(sys.argv[i + 1])
        if SIMULATED_NOW.tzinfo is None:
            SIMULATED_NOW = SIMULATED_NOW.replace(tzinfo=LOCAL_TZ)


def now_local():
    return SIMULATED_NOW if SIMULATED_NOW else datetime.datetime.now(LOCAL_TZ)


# === Notion helpers =========================================================

def query_reminders_on_pages(database_id):
    """Fetch every page in the DB where Reminders On is checked."""
    payload = {
        "filter": {"property": "Reminders On", "checkbox": {"equals": True}},
        "page_size": 100,
    }
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


def get_page_title(page):
    """Find the title property (key varies by DB) and return its plain text."""
    for prop_name, prop_val in page.get("properties", {}).items():
        if prop_val.get("type") == "title":
            return "".join(
                t.get("plain_text", "") for t in prop_val.get("title", [])
            ).strip()
    return "<no title>"


def get_checkbox(page, name):
    p = page.get("properties", {}).get(name, {})
    return bool(p.get("checkbox", False))


def get_select(page, name, default=None):
    p = page.get("properties", {}).get(name, {})
    sel = p.get("select")
    return sel["name"] if sel and sel.get("name") else default


def get_multi_select(page, name):
    p = page.get("properties", {}).get(name, {})
    return [o.get("name") for o in p.get("multi_select", []) if o.get("name")]


def get_rich_text(page, name, default=""):
    p = page.get("properties", {}).get(name, {})
    return "".join(t.get("plain_text", "") for t in p.get("rich_text", [])).strip() or default


def get_number(page, name, default=0):
    p = page.get("properties", {}).get(name, {})
    v = p.get("number")
    return v if v is not None else default


def get_datetime(page, name):
    """Return a tz-aware datetime in LOCAL_TZ for a date property, or None."""
    p = page.get("properties", {}).get(name, {})
    d = p.get("date")
    if not d or not d.get("start"):
        return None
    raw = d["start"]
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Plain date — treat as midnight local.
        dt = datetime.datetime.combine(dt.date(), datetime.time(0, 0), tzinfo=LOCAL_TZ)
    else:
        dt = dt.astimezone(LOCAL_TZ)
    return dt


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


# === Time helpers ===========================================================

def in_active_hours(dt):
    """8am–1:59am window."""
    h = dt.hour
    return h >= ACTIVE_START_HOUR or h < ACTIVE_END_HOUR_EXCLUSIVE


# Custom time parser: accepts "2 PM", "9:15 AM", "14:00", "9:15", etc.
TIME_RE = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])?\s*$"
)


def parse_custom_time(text):
    """Returns (hour, minute) in 24-hour form, or None if unparseable."""
    if not text:
        return None
    m = TIME_RE.match(text)
    if not m:
        return None
    h = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    period = (m.group(3) or "").upper()

    if period == "PM":
        if h < 12:
            h += 12
    elif period == "AM":
        if h == 12:
            h = 0
    # No period: assume 24-hour if h is unambiguous; if 1-11, treat as written
    # (i.e., "9:15" without AM/PM means 9:15am, consistent with most people's
    # default mental model for these casual entries).
    if not (0 <= h <= 23) or not (0 <= minute <= 59):
        return None
    return h, minute


# Map "Once per day (X)" frequency to its (hour, minute) fire time.
SCHEDULED_FIRE_TIMES = {
    "Once per day (morning)": (9, 0),
    "Once per day (midday)": (15, 0),
    "Once per day (evening)": (20, 0),
}


def get_scheduled_first_fire(frequency, custom_time_text):
    """Returns the local (hour, minute) the FIRST notification should fire,
    based on Reminder Frequency. Used to know when escalation cycle begins.
    Returns None if frequency invalid or custom time unparseable."""
    if frequency == "Custom time":
        return parse_custom_time(custom_time_text)
    return SCHEDULED_FIRE_TIMES.get(frequency)


# === Escalation schedule for High priority ==================================
# After the FIRST send, subsequent sends fire at these GAPS (in minutes) from
# the previous send. So with first send at 20:00:
#   send 1: 20:00       (first)
#   send 2: 20:00 + 120 = 22:00
#   send 3: 22:00 + 60  = 23:00
#   send 4: 23:00 + 30  = 23:30
#   send 5: 23:30 + 15  = 23:45
#   send 6: 23:45 + 7   = 23:52  (final)
ESCALATION_GAPS_MIN = [120, 60, 30, 15, 7]
MAX_HIGH_SENDS = len(ESCALATION_GAPS_MIN) + 1  # +1 for the first send itself


def next_high_send_due(last_sent_at, send_count):
    """For a High-priority reminder that has fired `send_count` times so far
    (with `last_sent_at` being the time of the last send), return the
    datetime when send #(send_count + 1) is due. Returns None if escalation
    is exhausted."""
    if send_count >= MAX_HIGH_SENDS:
        return None
    if send_count == 0:
        # No sends yet — caller handles "first send" logic elsewhere.
        return None
    gap_index = send_count - 1  # send_count=1 → gap_index 0 (120 min until send 2)
    if gap_index >= len(ESCALATION_GAPS_MIN):
        return None
    return last_sent_at + datetime.timedelta(minutes=ESCALATION_GAPS_MIN[gap_index])


# === Pushover ===============================================================

def send_pushover(title, message, priority):
    """priority: 0 (normal) or 1 (high). Emergency unused in new design."""
    payload = {
        "token": PUSHOVER_API_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "title": title,
        "message": message,
        "priority": priority,
        "url": NOTIFICATION_URL,
        "url_title": "Open Quick Habit Tracker",
    }
    if DRY_RUN:
        print(f"  [DRY RUN] PUSHOVER priority={priority} title={title!r}")
        return True
    resp = requests.post(PUSHOVER_URL, data=payload)
    if not resp.ok:
        print(f"  ⚠️  Pushover failed: {resp.status_code} {resp.text}")
        return False
    return True


# === Decision: should this page fire RIGHT NOW? =============================

def evaluate_triggers(page):
    """Returns the set of trigger names currently active for this page.

    Reads any property named `Trigger: <name> (Auto)` as a checkbox, AND
    intersects with the page's `Reminder Triggers` multi-select (only the
    triggers the user opted into for this page count).
    """
    opted_in = set(get_multi_select(page, "Reminder Triggers"))

    if VERBOSE:
        print(f"    [verbose] opted_in multi-select: {opted_in}")
        trigger_props = {
            k: v.get("checkbox", "<not a checkbox>")
            for k, v in page.get("properties", {}).items()
            if k.startswith("Trigger: ")
        }
        print(f"    [verbose] Trigger:* properties seen: {trigger_props}")

    if not opted_in:
        return set()

    active = set()
    for prop_name, prop_val in page.get("properties", {}).items():
        if not prop_name.startswith("Trigger: ") or not prop_name.endswith(" (Auto)"):
            continue
        trigger_name = prop_name[len("Trigger: "):-len(" (Auto)")]
        if VERBOSE:
            in_opt = trigger_name in opted_in
            checked = prop_val.get("checkbox", False)
            print(f"    [verbose]   parsed name={trigger_name!r} "
                  f"in_opted_in={in_opt} checked={checked}")
        if trigger_name in opted_in and prop_val.get("checkbox", False):
            active.add(trigger_name)
    return active


def should_fire_now(page, now):
    """Returns (should_fire, reason) for logging."""
    if not in_active_hours(now):
        return False, "outside active hours (8am-1am)"

    active_triggers = evaluate_triggers(page)
    if not active_triggers:
        return False, "no triggers active"

    if get_checkbox(page, "Reminder Acknowledged"):
        return False, "manually acknowledged"

    frequency = get_select(page, "Reminder Frequency")
    custom_time = get_rich_text(page, "Custom Time", "")
    fire_time = get_scheduled_first_fire(frequency, custom_time)
    if fire_time is None:
        return False, f"unparseable frequency/time: freq={frequency!r} custom={custom_time!r}"

    priority_name = get_select(page, "Reminder Priority", "Normal")
    send_count = int(get_number(page, "Reminder Send Count (Auto)", 0))
    last_sent_at = get_datetime(page, "Reminder Last Sent At (Auto)")

    fire_hour, fire_min = fire_time
    scheduled_today = now.replace(hour=fire_hour, minute=fire_min, second=0, microsecond=0)

    # Has today's scheduled fire time passed?
    if now < scheduled_today:
        return False, f"before scheduled time {fire_hour:02d}:{fire_min:02d}"

    # If we've never fired today (or last send was on a prior day), the FIRST send is due.
    # We compare against scheduled_today.date() because escalation cycle resets daily.
    if not last_sent_at or last_sent_at.date() < now.date():
        return True, "first send of the day"

    # We've already fired at least once today.
    if priority_name != "High":
        # Normal priority sends only once per day.
        return False, "Normal priority, already sent today"

    # High priority — escalation cycle.
    next_due = next_high_send_due(last_sent_at, send_count)
    if next_due is None:
        return False, "escalation exhausted"
    if now >= next_due:
        return True, f"escalation send #{send_count + 1}"
    return False, f"escalation send #{send_count + 1} due at {next_due.strftime('%H:%M')}"


# === Reset logic ============================================================

def maybe_reset_state(page):
    """If triggers are no longer active OR user acknowledged, reset send count.

    This is what makes 'escalation stops when trigger resolves' actually work:
    next time triggers come back, the page starts fresh at send 1.
    """
    active = evaluate_triggers(page)
    acknowledged = get_checkbox(page, "Reminder Acknowledged")
    current_count = int(get_number(page, "Reminder Send Count (Auto)", 0))

    if current_count == 0:
        return  # already clean

    if not active or acknowledged:
        update_page(page["id"], {
            "Reminder Send Count (Auto)": {"number": 0},
        })


# === Notification text ======================================================

def build_notification(page, title, active_triggers):
    if "Today Not Yet Done" in active_triggers:
        return f"Still haven't done: {title}", "Tap to open the Quick Habit Tracker."
    if "Missed Last Instance" in active_triggers:
        return f"Don't miss twice: {title}", "Tap to get back on the wagon."
    if "Behind on Weekly Target" in active_triggers:
        return f"Behind on {title}", "You're past midweek and below half target."
    # Generic fallback for future triggers
    triggers_str = ", ".join(sorted(active_triggers))
    return f"Reminder: {title}", f"Triggers: {triggers_str}"


# === Main ===================================================================

def process_page(page, now):
    title = get_page_title(page)
    # Log using a stable handle so public Actions logs don't leak page titles.
    # The title is still used internally to compose the Pushover notification.
    print(f"\n• {page_handle(page['id'])}")

    # First: reset state if triggers have resolved.
    maybe_reset_state(page)

    fire, reason = should_fire_now(page, now)
    if not fire:
        print(f"  ↳ skip: {reason}")
        return False

    active = evaluate_triggers(page)
    notif_title, notif_body = build_notification(page, title, active)
    priority_name = get_select(page, "Reminder Priority", "Normal")
    priority_num = 1 if priority_name == "High" else 0

    if not send_pushover(notif_title, notif_body, priority_num):
        return False

    # Bump escalation state.
    current_count = int(get_number(page, "Reminder Send Count (Auto)", 0))
    last_sent = get_datetime(page, "Reminder Last Sent At (Auto)")

    # If last send was a prior day, count resets to 1; otherwise increments.
    if not last_sent or last_sent.date() < now.date():
        new_count = 1
    else:
        new_count = current_count + 1

    update_page(page["id"], {
        "Reminder Last Sent At (Auto)": {"date": {"start": now.isoformat()}},
        "Reminder Send Count (Auto)": {"number": new_count},
    })
    print(f"  ✓ sent (priority={priority_name}, send #{new_count})")
    return True


def main():
    if not all([NOTION_TOKEN, PUSHOVER_USER_KEY, PUSHOVER_API_TOKEN]):
        print("Missing required env vars (NOTION_TOKEN, PUSHOVER_USER_KEY, PUSHOVER_API_TOKEN).")
        sys.exit(1)

    if not REMINDER_TARGET_DB_IDS:
        print("No databases configured. Set REMINDER_TARGET_DB_IDS env var.")
        sys.exit(1)

    if DRY_RUN:
        print("=== DRY RUN MODE — no Pushover sends, no Notion writes ===")
    if SIMULATED_NOW:
        print(f"=== SIMULATED TIME: {SIMULATED_NOW.isoformat()} ===")

    now = now_local()
    print(f"Reminder dispatcher run at {now.isoformat()}")
    print(f"Active hours window: yes" if in_active_hours(now) else "Active hours window: NO — will skip all sends")
    print(f"Target databases: {len(REMINDER_TARGET_DB_IDS)}")

    fired = 0
    skipped = 0
    for db_id in REMINDER_TARGET_DB_IDS:
        pages = query_reminders_on_pages(db_id)
        print(f"\n--- DB {db_id}: {len(pages)} pages with Reminders On ---")
        for page in pages:
            if process_page(page, now):
                fired += 1
            else:
                skipped += 1

    print(f"\nDone. Fired: {fired}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
