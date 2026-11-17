"""Trial-reel bot that runs on this computer (no Meta API needed).

    python local_bot.py run              hourly job: sync when due, post the trials owed this hour
    python local_bot.py sync             only read reels + views from instagram.com
    python local_bot.py post CODE        post one trial now from the reel with this shortcode
    python local_bot.py report           print the daily summary and write DASHBOARD.md

Reels and views come from instagram.com in the robot's Edge window (open_browser.py);
posting goes through the Instagram app on an Android phone (device.py + phone.py).
State is plain JSON in ~/trial-bot-data (never committed).
"""
import json
import math
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict

import bot  # reuse the schedule / reporting helpers and config
import device
import igweb
import phone
from bot import CONFIG, DEFAULT_HOURS, hours_since, iso, local, now
from remix import remix

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.expanduser("~"), "trial-bot-data")
STATE_PATH = os.path.join(DATA, "state.json")
USERNAME = CONFIG.get("username", "hillelgf")


# --- state ---------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_PATH):
        return json.load(open(STATE_PATH, encoding="utf-8"))
    return {"originals": {}, "sources": {}, "posts": [], "log": []}


def save_state(state):
    os.makedirs(DATA, exist_ok=True)
    state["log"] = state["log"][-500:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def log(state, msg):
    line = f"{iso(now())} {msg}"
    print(line, flush=True)
    state["log"].append(line)


def notify(state, text):
    bot.notify(state, text)



# --- schedule (same rules as the cloud version) ----------------------------

def ranked_hours(state):
    samples = defaultdict(list)
    group = [(o["taken_at"], o["plays"]) for o in state["originals"].values()
             if o.get("taken_at") and o.get("plays")]
    if group:
        med = statistics.median(v for _, v in group) or 1
        for ts, v in group:
            samples[local(iso_from_epoch(ts)).hour].append(v / med)
    scored = {h: statistics.median(s) for h, s in samples.items() if len(s) >= CONFIG["min_reels_per_hour"]}
    known = sorted(scored, key=scored.get, reverse=True)
    return known + [h for h in DEFAULT_HOURS if h not in scored], scored


def iso_from_epoch(ts):
    from datetime import datetime, timezone
    return iso(datetime.fromtimestamp(ts, timezone.utc))


def hour_plan(state):
    target = bot.daily_target(state)
    ranked, _ = ranked_hours(state)
    plan = defaultdict(int)
    for i in range(target):
        plan[ranked[i % 24]] += 1
    return dict(plan)


def posts_owed(state):
    plan = hour_plan(state)
    today, hour = local().date(), local().hour
    due = sum(n for h, n in plan.items() if h <= hour)
    done = sum(1 for p in state["posts"] if local(p["posted_at"]).date() == today)
    return max(0, due - done)


# --- sync --------------------------------------------------------------------

def cmd_sync(state):
    with igweb.Web() as web:
        _sync(state, web)


def _sync(state, web):
    if not web.logged_in():
        raise RuntimeError("the robot browser is logged out of Instagram")
    full = len(state["originals"]) < 300
    known = set(state["originals"])
    reels = web.list_reels(USERNAME, max_scrolls=150 if full else 6,
                             stop_after_known=None if full else known)
    for code, r in reels.items():
        o = state["originals"].setdefault(code, {})
        o.update(r, seen_at=iso(now()))
    log(state, f"sync: read {len(reels)} reels ({len(state['originals'])} known)")

    pick_sources(state)

    # captions + post times for the pool (and the best reels, for the hour ranking), a few per sync
    want = [c for c, s in state["sources"].items() if s.get("active")]
    want += sorted(state["originals"], key=lambda c: state["originals"][c].get("plays") or 0, reverse=True)[:150]
    budget = 25
    for code in dict.fromkeys(want):
        if budget <= 0:
            break
        o = state["originals"][code]
        if o.get("details_at"):
            continue
        det = web.reel_details(code)
        o["details_at"] = iso(now())
        if det:
            o.update(caption=det["caption"], taken_at=det["taken_at"])
        else:
            o["no_video"] = True
        budget -= 1
        time.sleep(random.uniform(2, 5))

    update_generations(state)
    state["last_sync"] = iso(now())


def pick_sources(state):
    originals = state["originals"]
    path = os.path.join(ROOT, "sources.txt")
    wanted = []
    if os.path.exists(path):
        wanted = [bot.shortcode(l) for l in open(path, encoding="utf-8")
                  if l.strip() and not l.lstrip().startswith("#")]
    codes = [c for c in wanted if c in originals]
    pool = max(CONFIG["min_pool"],
               math.ceil(bot.daily_target(state) * CONFIG["min_days_between_reposts"] * CONFIG["pool_margin"]))
    blocked = {c for c, s in state["sources"].items() if s["retired"] or s.get("broken")}
    blocked |= {c for c, o in originals.items() if o.get("no_video")}
    ranked = sorted((c for c, o in originals.items()
                     if c not in codes and c not in blocked
                     and (o.get("plays") or 0) >= CONFIG["min_views_for_auto"]),
                    key=lambda c: originals[c].get("plays") or 0, reverse=True)
    codes += ranked[:max(0, pool - len([c for c in codes if c not in blocked]))]
    for rank, c in enumerate(codes, 1):
        s = state["sources"].setdefault(c, {"generation": 0, "retired": False, "last_posted": None})
        s.update(rank=rank, active=True)
    for c, s in state["sources"].items():
        if c not in codes:
            s["active"] = False


def update_generations(state):
    """Trial view counts aren't readable without the API, so a reel moves to the next
    remix round after max_posts_per_generation posts, and retires after max_generations."""
    for c, s in state["sources"].items():
        if s["retired"]:
            continue
        n = sum(1 for p in state["posts"] if p["source"] == c and p["generation"] == s["generation"])
        if n >= CONFIG["max_posts_per_generation"]:
            s["generation"] += 1
            if s["generation"] > CONFIG["max_generations"]:
                s["retired"] = True
                log(state, f"{c} retired after {CONFIG['max_generations']} remix rounds")
            else:
                log(state, f"{c} -> remix round {s['generation']}")


# --- posting -----------------------------------------------------------------

def post_code(state, web, code, serial):
    s = state["sources"].setdefault(code, {"generation": 0, "retired": False, "last_posted": None})
    o = state["originals"].setdefault(code, {})
    det = web.reel_details(code)  # fresh link every time; Instagram's file links expire
    if not det:
        s["broken"] = True
        log(state, f"{code}: no downloadable video, skipping")
        return False
    caption = det["caption"] or o.get("caption", "")
    with tempfile.TemporaryDirectory(dir=DATA) as tmp:
        src, out = os.path.join(tmp, "src.mp4"), os.path.join(tmp, "out.mp4")
        igweb.download(det["video_url"], src)
        info = remix(src, out, s["generation"], random.Random())
        phone.use(serial)
        phone.push_video(out, f"{code}_g{s['generation']}")
        try:
            phone.Poster(log=lambda m: log(state, m)).post_trial(caption)
        finally:
            phone.clear_pushed()
    state["posts"].append({"source": code, "generation": s["generation"], "posted_at": iso(now()),
                           "letter": info["letter"]})
    s["last_posted"] = iso(now())
    state.setdefault("started_at", iso(now()))
    log(state, f"posted trial from {code} (round {s['generation']})")
    update_generations(state)
    return True


def cmd_run(state):
    sync_due = hours_since(state.get("last_sync")) >= CONFIG["sync_every_hours"]
    owed = min(posts_owed(state), CONFIG["max_posts_per_run"]) if CONFIG["enabled"] else 0
    if sync_due or owed:
        with igweb.Web() as web:
            if sync_due:
                _sync(state, web)
                save_state(state)
            owed = min(posts_owed(state), CONFIG["max_posts_per_run"]) if CONFIG["enabled"] else 0
            if owed:
                _post_owed(state, web, owed)
    if local().hour >= CONFIG["daily_report_hour"] and state.get("report_date") != str(local().date()):
        notify(state, report(state))
        state["report_date"] = str(local().date())


def _post_owed(state, web, owed):
    with device.posting_phone(log=lambda m: log(state, m)) as serial:
        for _ in range(owed):
            nxt = bot.next_sources(state)
            if not nxt:
                log(state, "nothing to post: every reel in the pool is waiting or retired")
                break
            if not post_code(state, web, nxt[0], serial):
                continue
            save_state(state)


def report(state):
    today = local().date()
    today_posts = [p for p in state["posts"] if local(p["posted_at"]).date() == today]
    plan = hour_plan(state)
    active = [c for c, s in state["sources"].items() if s.get("active")]
    lines = [
        f"📊 סיכום יומי {today:%d/%m}",
        f"עלו היום: {len(today_posts)} מתוך יעד {bot.daily_target(state)}",
        f"סה\"כ טריילים: {len(state['posts'])}",
        f"שעות העלאה: {', '.join(f'{h:02d}' for h in sorted(plan))}",
        f"סרטונים במאגר: {len(active)} (בסבב עריכה: "
        f"{sum(1 for c in active if state['sources'][c]['generation'] > 0)})",
    ]
    return "\n".join(lines)


def write_dashboard(state):
    lines = ["# דשבורד טריילים", "", report(state), "",
             "| # | רילז | צפיות מקור | סבב | טריילים | אחרון |", "|---|---|---|---|---|---|"]
    for c, s in sorted(state["sources"].items(), key=lambda kv: kv[1].get("rank", 999)):
        if s.get("active"):
            n = sum(1 for p in state["posts"] if p["source"] == c)
            lines.append(f"| {s.get('rank')} | https://www.instagram.com/reel/{c}/ | "
                         f"{bot.fmt(state['originals'].get(c, {}).get('plays'))} | {s['generation']} | {n} | "
                         f"{s.get('last_posted') or '—'} |")
    lines += ["", "```", *state["log"][-40:], "```"]
    with open(os.path.join(DATA, "DASHBOARD.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("run", "sync", "post", "report"):
        sys.exit(__doc__)
    os.makedirs(DATA, exist_ok=True)
    state = load_state()
    try:
        if cmd == "sync":
            cmd_sync(state)
        elif cmd == "run":
            cmd_run(state)
        elif cmd == "post":
            with igweb.Web() as web, device.posting_phone(log=lambda m: log(state, m)) as serial:
                post_code(state, web, bot.shortcode(sys.argv[2]), serial)
        print(report(state))
    except Exception as e:
        log(state, f"ERROR in {cmd}: {e}")
        notify(state, f"❌ שגיאה בבוט: {e}")
        raise
    finally:
        save_state(state)
        write_dashboard(state)


if __name__ == "__main__":
    main()
