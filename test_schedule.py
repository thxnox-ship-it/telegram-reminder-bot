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

print("\n" + "=" * 66)
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {_failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
print("=" * 66)
