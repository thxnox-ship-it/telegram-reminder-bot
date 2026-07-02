"""
Integration test for the reminder bot.

Unlike a unit test with re-implemented logic, this imports the ACTUAL functions
from bot.py and drives the real send_reminders()/catch_up() jobs across a
simulated multi-month timeline. It monkeypatches three things:

  - the datastore (in-memory dict instead of the JSON file)
  - the clock (bot.datetime -> a controllable "now")
  - the Telegram send call (records messages instead of hitting the network)

Run:  python3 test_schedule.py   (exits non-zero if any check fails)
"""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta

os.environ.setdefault("BOT_TOKEN", "TEST:TOKEN")
os.environ["DATA_FILE"] = "/tmp/telegram-reminder-test.json"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

SGT = bot.SGT

# ── in-memory datastore ────────────────────────────────────────────────────
_STORE = {}


def _clone(cfg):
    return {k: {kk: [dict(r) for r in vv] if kk == "reminders" else vv
                for kk, vv in v.items()} for k, v in cfg.items()}


bot.load_config = lambda: _clone(_STORE)


def _save(cfg):
    _STORE.clear()
    _STORE.update(_clone(cfg))


bot.save_config = _save

# ── clock control ──────────────────────────────────────────────────────────
_NOW = {"dt": None}
_real_dt = bot.datetime


class _FakeDatetime:
    @staticmethod
    def now(tz=None):
        return _NOW["dt"]

    @staticmethod
    def fromisoformat(s):
        return _real_dt.fromisoformat(s)

    @staticmethod
    def strptime(s, fmt):
        return _real_dt.strptime(s, fmt)


bot.datetime = _FakeDatetime

# ── fake bot / context / job ───────────────────────────────────────────────
_SENT = []


class _FakeBot:
    async def send_message(self, chat_id, text):
        _SENT.append((_NOW["dt"].date(), _NOW["dt"].hour, chat_id, text))


class _Ctx:
    def __init__(self, hour=None):
        self.bot = _FakeBot()
        self.job = type("J", (), {"data": {"hour": hour}})()


def _set_now(d, h):
    _NOW["dt"] = datetime(d.year, d.month, d.day, h, 0, tzinfo=SGT)


def seed(chat_id, days, hour, message, months, start):
    end = bot.add_months(start, months)
    _STORE[str(chat_id)] = {"reminders": [{
        "id": 1, "days": days, "hour": hour, "message": message,
        "paused": False, "end_date": end.isoformat(), "last_sent": None,
    }]}
    return end


def seed_typed(chat_id, rtype, hour, message, end_date, **schedule):
    """Seed a typed reminder (weekly / monthly_weekday / quarterly)."""
    reminder = {
        "id": 1, "type": rtype, "hour": hour, "message": message,
        "paused": False,
        "end_date": end_date.isoformat() if end_date else None,
        "last_sent": None,
    }
    reminder.update(schedule)
    _STORE[str(chat_id)] = {"reminders": [reminder]}


def run_timeline(start, end, hours=bot.SEND_HOURS):
    """Advance day-by-day, running the real daily job at each slot."""
    day = start
    while day <= end + timedelta(days=5):
        for h in hours:
            _set_now(day, h)
            asyncio.run(bot.send_reminders(_Ctx(h)))
        day += timedelta(days=1)


_failures = []


def check(name, ok, detail=""):
    mark = "✅" if ok else "❌"
    print(f"{mark} {'PASS' if ok else 'FAIL'}: {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        _failures.append(name)


def scenario(title, chat_id, days, hour, message, months, start):
    _STORE.clear(); _SENT.clear()
    end = seed(chat_id, days, hour, message, months, start)
    run_timeline(start, end)
    fired = [s for s in _SENT if s[2] == chat_id]
    print(f"\n{'='*66}\n{title}\n  days={days} hour={hour} months={months} "
          f"start={start} end={end}\n{'='*66}")
    for d, h, _cid, txt in fired:
        final = "  <FINAL>" if "final reminder" in txt else ""
        print(f"  {d.strftime('%a %d %b %Y')}  {h:02d}:00{final}")
    return fired, end


# 1. single day, 3 months, noon
fired, end = scenario("1. Day 1 monthly, 3 months @ 12:00", 100, [1], 12,
                      "Monthly!", 3, date(2026, 7, 1))
check("fires Jul/Aug/Sep/Oct (4 sends up to end)", len(fired) == 4, f"{len(fired)}")
check("all at hour 12", all(h == 12 for _, h, _, _ in fired))
check("exactly one FINAL", sum("final reminder" in t for *_, t in fired) == 1)
check("nothing after end_date", all(d <= end for d, *_ in fired))

# 2. hour filtering
fired, _ = scenario("2. Day 1 @ 09:00 — hour filter", 101, [1], 9,
                    "Morning", 2, date(2026, 7, 1))
check("only 09:00 sends", all(h == 9 for _, h, _, _ in fired))

# 2b/2c. new 3 PM / 6 PM slots fire correctly and don't leak into other hours
fired, _ = scenario("2b. Day 1 @ 15:00 — coffee slot", 107, [1], 15,
                    "Coffee time", 2, date(2026, 7, 1))
check("2b. only 15:00 sends", bool(fired) and all(h == 15 for _, h, _, _ in fired))

fired, _ = scenario("2c. Day 1 @ 18:00 — dinner slot", 108, [1], 18,
                    "Dinner time", 2, date(2026, 7, 1))
check("2c. only 18:00 sends", bool(fired) and all(h == 18 for _, h, _, _ in fired))

check("2d. HOUR_LABELS and HOUR_EMOJI cover every SEND_HOURS entry",
      all(h in bot.HOUR_LABELS and h in bot.HOUR_EMOJI for h in bot.SEND_HOURS))
check("2e. SEND_HOURS is exactly the five expected slots",
      bot.SEND_HOURS == (9, 12, 15, 18, 21), str(bot.SEND_HOURS))

# 3. end-of-month clamp (day 31 across Feb)
fired, _ = scenario("3. Day 31 across Feb (clamp)", 102, [31], 12,
                    "EOM", 3, date(2026, 1, 15))
check("day 31 fires in Feb (clamped to 28)",
      any(d.month == 2 and d.day == 28 for d, *_ in fired))

# 4. paused never fires
_STORE.clear(); _SENT.clear()
seed(103, [1], 12, "Paused", 2, date(2026, 7, 1))
_STORE["103"]["reminders"][0]["paused"] = True
run_timeline(date(2026, 7, 1), date(2026, 9, 1))
check("4. paused reminder never fires", not any(s[2] == 103 for s in _SENT))

# 5. dedupe — same slot run twice on the same day sends once
_STORE.clear(); _SENT.clear()
seed(104, [1], 12, "Once", 2, date(2026, 7, 1))
_set_now(date(2026, 7, 1), 12)
asyncio.run(bot.send_reminders(_Ctx(12)))
asyncio.run(bot.send_reminders(_Ctx(12)))
check("5. dedupe: same slot twice => one send",
      len([s for s in _SENT if s[2] == 104]) == 1)

# 6. catch-up — bot boots at 14:00 after missing the 12:00 slot
_STORE.clear(); _SENT.clear()
seed(105, [1], 12, "Missed-me", 2, date(2026, 7, 1))
_set_now(date(2026, 7, 1), 14)  # 14:00, 12:00 job never ran
asyncio.run(bot.catch_up(_Ctx()))
check("6. catch-up delivers the missed 12:00 slot",
      len([s for s in _SENT if s[2] == 105]) == 1)
# ...and running catch-up again (or the daily job later) doesn't re-send
asyncio.run(bot.catch_up(_Ctx()))
_set_now(date(2026, 7, 1), 12)
asyncio.run(bot.send_reminders(_Ctx(12)))
check("6b. catch-up + daily job don't double-send",
      len([s for s in _SENT if s[2] == 105]) == 1)

# 7. extend an ended reminder makes it fire again
_STORE.clear(); _SENT.clear()
start = date(2026, 7, 1)
end = seed(106, [1], 12, "Extend-me", 1, start)  # ends 2026-08-01
run_timeline(start, end)
before = len([s for s in _SENT if s[2] == 106])
# now "extend": set new end 2 months from a later 'today', clear last_sent
_set_now(date(2026, 9, 1), 0)
new_end = bot.add_months(date(2026, 9, 1), 2)
bot.update_reminder(106, 1, end_date=new_end.isoformat(), paused=False, last_sent=None)
run_timeline(date(2026, 9, 1), new_end)
after = len([s for s in _SENT if s[2] == 106])
check("7. extending an ended reminder fires again", after > before,
      f"{before} -> {after}")

# 8. next_fire_date sanity
_STORE.clear()
today = date(2026, 7, 10)
r_future = {"days": [20], "hour": 12, "paused": False,
            "end_date": bot.add_months(today, 3).isoformat(), "last_sent": None}
nf = bot.next_fire_date(r_future, today)
check("8. next_fire_date returns upcoming day-20", nf == date(2026, 7, 20), str(nf))
r_paused = dict(r_future, paused=True)
check("8b. next_fire_date is None when paused", bot.next_fire_date(r_paused, today) is None)

# ── new schedule types ─────────────────────────────────────────────────────

# 9. weekly — every Wednesday for 2 months
_STORE.clear(); _SENT.clear()
start, end = date(2026, 7, 1), date(2026, 9, 1)
seed_typed(200, "weekly", 12, "Weekly!", end, weekday=2)  # Wed = 2
run_timeline(start, end)
fired = [s for s in _SENT if s[2] == 200]
expected_weds = sum(
    1 for i in range((end - start).days + 1)
    if (start + timedelta(days=i)).weekday() == 2
)
check("9. weekly fires on every Wednesday", len(fired) == expected_weds,
      f"{len(fired)} vs {expected_weds}")
check("9b. weekly only fires on Wednesdays", all(d.weekday() == 2 for d, *_ in fired))
check("9c. weekly gets exactly one FINAL",
      sum("final reminder" in t for *_, t in fired) == 1)

# 10. monthly by weekday — first Monday, 3 months
_STORE.clear(); _SENT.clear()
start, end = date(2026, 7, 1), date(2026, 10, 1)
seed_typed(201, "monthly_weekday", 9, "First Mon", end, ordinal="first", weekday=0)
run_timeline(start, end)
fired = [s for s in _SENT if s[2] == 201]
check("10. first-Monday fires once per month",
      len(fired) == 3, f"{len(fired)}: {[d.isoformat() for d, *_ in fired]}")
check("10b. all are Mondays in days 1-7",
      all(d.weekday() == 0 and d.day <= 7 for d, *_ in fired))
check("10e. monthly_weekday gets exactly one FINAL message",
      sum("final reminder" in t for *_, t in fired) == 1)
check("10f. FINAL is on the last (3rd) send, and reminder is auto-paused after",
      "final reminder" in fired[-1][3] and _STORE["201"]["reminders"][0]["paused"] is True)

# 10c. last Friday of the month
_STORE.clear(); _SENT.clear()
start, end = date(2026, 7, 1), date(2026, 10, 1)
seed_typed(202, "monthly_weekday", 21, "Last Fri", end, ordinal="last", weekday=4)
run_timeline(start, end)
fired = [s for s in _SENT if s[2] == 202]
check("10c. last-Friday: all Fridays in the final 7 days of the month",
      len(fired) >= 3 and all(
          d.weekday() == 4 and d.day > bot.calendar.monthrange(d.year, d.month)[1] - 7
          for d, *_ in fired))

# 10d. second-last Tuesday
_STORE.clear(); _SENT.clear()
start, end = date(2026, 7, 1), date(2026, 9, 1)
seed_typed(203, "monthly_weekday", 12, "2nd-last Tue", end, ordinal="second_last", weekday=1)
run_timeline(start, end)
fired = [s for s in _SENT if s[2] == 203]
ok = all(
    d.day == [x for x in range(1, bot.calendar.monthrange(d.year, d.month)[1] + 1)
              if date(d.year, d.month, x).weekday() == 1][-2]
    for d, *_ in fired)
check("10d. second-last Tuesday matches computed dates", len(fired) >= 2 and ok)

# 11. quarterly — starts 30 Nov 2026, 12 months, Feb clamp
_STORE.clear(); _SENT.clear()
start = date(2026, 11, 30)
end = bot.add_months(start, 12)  # 30 Nov 2027
seed_typed(204, "quarterly", 12, "Quarterly", end, start_date=start.isoformat())
run_timeline(date(2026, 11, 1), end)
fired = [(d, h) for d, h, cid, _ in _SENT if cid == 204]
expected = [date(2026, 11, 30), date(2027, 2, 28), date(2027, 5, 30),
            date(2027, 8, 30), date(2027, 11, 30)]
check("11. quarterly fires every 3 months incl. Feb clamp",
      [d for d, _ in fired] == expected,
      f"{[d.isoformat() for d, _ in fired]}")

# 11b. quarterly next_fire_date before the start date is the start date
r_q = {"type": "quarterly", "start_date": "2027-03-15", "hour": 12,
       "paused": False, "end_date": None, "last_sent": None}
check("11b. quarterly next fire before start = start",
      bot.next_fire_date(r_q, date(2026, 7, 10)) == date(2027, 3, 15))

# 12. indefinite (end_date None) — keeps firing, never FINAL
_STORE.clear(); _SENT.clear()
seed_typed(205, "weekly", 12, "Forever", None, weekday=0)
run_timeline(date(2026, 7, 1), date(2026, 9, 30))
fired = [s for s in _SENT if s[2] == 205]
check("12. indefinite reminder keeps firing", len(fired) >= 12, f"{len(fired)}")
check("12b. indefinite never sends FINAL",
      not any("final reminder" in t for *_, t in fired))
check("12c. indefinite reminder not auto-paused",
      not _STORE["205"]["reminders"][0]["paused"])

# 13. every send starts with the reminder header
_STORE.clear(); _SENT.clear()
seed(206, [1], 12, "Check header", 1, date(2026, 7, 1))
run_timeline(date(2026, 7, 1), date(2026, 8, 1))
fired = [s for s in _SENT if s[2] == 206]
check("13. sends start with the 🙌 REMINDER: header",
      bool(fired) and all(t.startswith(bot.REMINDER_HEADER + "\n") for *_, t in fired))

# 14. date parsing for the quarterly start-date prompt
cases = {
    "2026-08-15": date(2026, 8, 15),
    "15 Aug 2026": date(2026, 8, 15),
    "15 August 2026": date(2026, 8, 15),
    "15/08/2026": date(2026, 8, 15),
    "not a date": None,
    "32 Aug 2026": None,
}
check("14. _parse_date handles all supported formats",
      all(bot._parse_date(k) == v for k, v in cases.items()))

# ── stress test: frequency correctness ──────────────────────────────────────
#
# These checks build EXPECTED fire-dates from scratch (plain calendar
# arithmetic, not by calling bot.py's own scheduling helpers) and compare
# them against what the real send job actually fired. That way a bug shared
# between the "expected" and "actual" code paths can't hide.
import calendar as _cal


def ref_weekday_dates(weekday, start, end):
    out, d = [], start
    while d <= end:
        if d.weekday() == weekday:
            out.append(d)
        d += timedelta(days=1)
    return out


def ref_monthly_weekday_dates(ordinal, weekday, start, end):
    idx = {"first": 0, "second": 1, "second_last": -2, "last": -1}[ordinal]
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        last_day = _cal.monthrange(y, m)[1]
        occurrences = [d for d in range(1, last_day + 1)
                       if date(y, m, d).weekday() == weekday]
        dt = date(y, m, occurrences[idx])
        if start <= dt <= end:
            out.append(dt)
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out


print("\n" + "=" * 66 + "\nSTRESS TEST — frequency correctness\n" + "=" * 66)

# S1. Weekly: every weekday, spanning a year+ boundary, checked against a
# brute-force day-by-day scan.
for wd in range(7):
    _STORE.clear(); _SENT.clear()
    start = date(2026, 11, 15)
    end = bot.add_months(start, 13)  # crosses a year boundary
    seed_typed(300 + wd, "weekly", 9, f"wd{wd}", end, weekday=wd)
    run_timeline(start, end)
    fired_dates = sorted(d for d, h, cid, t in _SENT if cid == 300 + wd)
    expected = ref_weekday_dates(wd, start, end)
    check(f"S1.{wd} weekly({bot.WEEKDAY_LABELS[wd]}) matches brute-force scan",
          fired_dates == expected,
          f"{len(fired_dates)} vs {len(expected)}")

# S2. Monthly-by-weekday: every ordinal x weekday combo, spanning 14 months
# including a leap February (2028), checked against an independent
# per-month occurrence calculation.
for ordinal in ("first", "second", "second_last", "last"):
    for wd in range(7):
        _STORE.clear(); _SENT.clear()
        start = date(2027, 1, 1)
        end = bot.add_months(start, 14)  # through Feb 2028 (leap)
        seed_typed(400, "monthly_weekday", 21, "mw", end, ordinal=ordinal, weekday=wd)
        run_timeline(start, end)
        fired_dates = sorted(d for d, h, cid, t in _SENT if cid == 400)
        expected = ref_monthly_weekday_dates(ordinal, wd, start, end)
        detail = "" if fired_dates == expected else f"{fired_dates} vs {expected}"
        check(f"S2.{ordinal}.{wd} monthly_weekday matches independent calc",
              fired_dates == expected, detail)

# S3. Quarterly across a leap-year Feb 29, hand-computed expected dates
# (not derived from bot.add_months, to avoid testing a function against
# itself).
_STORE.clear(); _SENT.clear()
start = date(2027, 11, 30)
end = bot.add_months(start, 15)  # 2029-02-28
seed_typed(401, "quarterly", 9, "q29", end, start_date=start.isoformat())
run_timeline(start, end)
fired_dates = sorted(d for d, h, cid, t in _SENT if cid == 401)
expected = [
    date(2027, 11, 30),
    date(2028, 2, 29),   # 2028 is a leap year — no clamp
    date(2028, 5, 30),
    date(2028, 8, 30),
    date(2028, 11, 30),
    date(2029, 2, 28),   # 2029 is not a leap year — clamped
]
check("S3. quarterly day-30 start crosses leap and non-leap Februarys correctly",
      fired_dates == expected, f"{fired_dates}")

print("\n" + "=" * 66 + "\nSTRESS TEST — duration / months-count correctness\n" + "=" * 66)


# ── minimal fakes to drive the actual interactive setup_callback handler ────
class _FakeChat:
    def __init__(self, id_, type_="private"):
        self.id = id_
        self.type = type_


class _FakeUser:
    def __init__(self, id_=1):
        self.id = id_


class _FakeQuery:
    def __init__(self, data, chat):
        self.data = data
        self.message = type("M", (), {"chat": chat})()
        self.edits = []

    async def answer(self, *a, **kw):
        pass

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append(text)


class _FakeUpdate:
    def __init__(self, query, chat, user):
        self.callback_query = query
        self.effective_chat = chat
        self.effective_user = user


class _FakeSetupCtx:
    def __init__(self, chat_data):
        self.chat_data = chat_data
        self.bot = _FakeBot()


async def press(data, chat_data, chat_id=999):
    chat = _FakeChat(chat_id)
    query = _FakeQuery(data, chat)
    update = _FakeUpdate(query, chat, _FakeUser())
    ctx = _FakeSetupCtx(chat_data)
    await bot.setup_callback(update, ctx)
    return ctx.chat_data


# S4. "months" duration picker sets the correct pending_end_date for a
# non-quarterly type (base = today).
_set_now(date(2026, 3, 10), 0)
for months in (1, 5, 12):
    cd = asyncio.run(press(f"setup:months:{months}", {"pending_type": "weekly", "pending_weekday": 2}))
    expected_end = bot.add_months(date(2026, 3, 10), months)
    check(f"S4.{months} weekly duration picker uses TODAY as base",
          cd.get("pending_end_date") == expected_end.isoformat(),
          f"{cd.get('pending_end_date')} vs {expected_end.isoformat()}")

# S5. For quarterly, the "months" duration picker must base itself on the
# chosen START DATE, not today (they can be far apart).
_set_now(date(2026, 3, 10), 0)
for months in (3, 9):
    cd = asyncio.run(press(
        f"setup:months:{months}",
        {"pending_type": "quarterly", "pending_start_date": date(2026, 8, 31).isoformat()},
    ))
    expected_end = bot.add_months(date(2026, 8, 31), months)
    check(f"S5.{months} quarterly duration picker uses START DATE as base",
          cd.get("pending_end_date") == expected_end.isoformat(),
          f"{cd.get('pending_end_date')} vs {expected_end.isoformat()}")

# S6. "Indefinite" always yields end_date None, for every type.
for rtype, extra in (
    ("monthly_date", {}),
    ("weekly", {"pending_weekday": 0}),
    ("monthly_weekday", {"pending_ordinal": "first", "pending_weekday": 0}),
    ("quarterly", {"pending_start_date": date(2026, 8, 31).isoformat()}),
):
    cd = asyncio.run(press("setup:months:inf", {"pending_type": rtype, **extra}))
    check(f"S6.{rtype} Indefinite sets pending_end_date to None",
          cd.get("pending_end_date") is None and "pending_end_date" in cd)

# S7. End-to-end: the number of months chosen in the picker is exactly the
# number of months the reminder actually keeps firing for, then stops.
for months in (1, 2, 6, 11):
    _STORE.clear(); _SENT.clear()
    start = date(2026, 5, 1)
    end = bot.add_months(start, months)
    seed_typed(500, "weekly", 9, "S7", end, weekday=start.weekday())
    run_timeline(start, end)
    fired = sorted(d for d, h, cid, t in _SENT if cid == 500)
    check(f"S7.{months} last fire is on/before the {months}-month end date",
          bool(fired) and fired[-1] <= end,
          f"last={fired[-1] if fired else None} end={end}")
    check(f"S7.{months}b exactly one FINAL message, on the last fire",
          sum("final reminder" in t for d, h, cid, t in _SENT if cid == 500) == 1
          and "final reminder" in next(t for d, h, cid, t in _SENT
                                       if cid == 500 and d == fired[-1]))
    # nothing fires again once end_date has passed
    run_timeline(end + timedelta(days=1), end + timedelta(days=30))
    check(f"S7.{months}c no sends after the reminder's own end_date",
          len([s for s in _SENT if s[2] == 500]) == len(fired))

print("\n" + "=" * 66)
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {_failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
print("=" * 66)
