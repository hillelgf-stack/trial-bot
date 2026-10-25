"""Trial-reel bot.

    python bot.py run      hourly job: sync stats when due, post the trials owed for this hour
    python bot.py sync     only pull reels + insights
    python bot.py report   decrypt state and write DASHBOARD.md locally (needs IG_TOKEN + STATE_KEY)

State lives encrypted in data/state.enc (the repo is public, the numbers are not).
"""
import base64
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet, InvalidToken

from ig import Instagram, IGError
from remix import remix

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "data", "state.enc")
CONFIG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
TZ = ZoneInfo(CONFIG["timezone"])
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# Used for hours with too little data: evening first, then midday.
DEFAULT_HOURS = [20, 21, 19, 18, 22, 13, 12, 14, 17, 16, 15, 11, 10, 9, 23, 8, 7, 0, 1, 6, 2, 3, 4, 5]


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts):
    return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def hours_since(ts):
    return (now() - parse(ts)).total_seconds() / 3600 if ts else float("inf")


def local(ts=None):
    return (parse(ts) if ts else now()).astimezone(TZ)


# --- encrypted state -------------------------------------------------------

def _fernet():
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(os.environ["STATE_KEY"].encode()).digest()))


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            return json.loads(_fernet().decrypt(open(STATE_PATH, "rb").read()))
        except InvalidToken:
            sys.exit("STATE_KEY does not match data/state.enc")
    return {"originals": {}, "sources": {}, "posts": [], "log": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state["log"] = state["log"][-300:]
    open(STATE_PATH, "wb").write(_fernet().encrypt(json.dumps(state, ensure_ascii=False).encode()))


def log(state, msg):
    line = f"{iso(now())} {msg}"
    print(line)
    state["log"].append(line)


def get_client(state):
    """The refreshed token is kept in the state; a new IG_TOKEN secret always wins."""
    seed = os.environ["IG_TOKEN"]
    seed_hash = hashlib.sha256(seed.encode()).hexdigest()[:16]
    tok = state.get("token") or {}
    if tok.get("seed") != seed_hash:
        tok = {"seed": seed_hash, "value": seed, "refreshed_at": iso(now())}
        state["token"] = tok
    ig = Instagram(tok["value"], CONFIG["api_version"])
    if hours_since(tok.get("refreshed_at")) > 24 * 7 and not DRY_RUN:
        try:
            tok["value"] = ig.refresh_token()
            tok["refreshed_at"] = iso(now())
            log(state, "token refreshed")
        except IGError as e:
            log(state, f"token refresh failed: {e}")
            notify(state, f"⚠️ חידוש הטוקן של אינסטגרם נכשל: {e}")
    return ig


# --- daily target & best hours ---------------------------------------------

def daily_target(state):
    """5 a day at first, +5 every week, up to 30."""
    r = CONFIG["ramp"]
    if not state.get("started_at"):
        return r["start"]
    weeks = int(hours_since(state["started_at"]) // (24 * r["every_days"]))
    return min(r["max"], r["start"] + r["step"] * weeks)


def ranked_hours(state):
    """Local hours ordered by how well reels posted at that hour performed (views vs median)."""
    samples = defaultdict(list)
    groups = [
        [(o["timestamp"], o["insights"]["views"]) for o in state["originals"].values()
         if (o.get("insights") or {}).get("views")],
        [(p["posted_at"], p["insights"]["views"]) for p in state["posts"]
         if (p.get("insights") or {}).get("views")],
    ]
    for group in groups:
        if not group:
            continue
        med = statistics.median(v for _, v in group) or 1
        for ts, v in group:
            samples[local(ts).hour].append(v / med)
    scored = {h: statistics.median(s) for h, s in samples.items() if len(s) >= CONFIG["min_reels_per_hour"]}
    known = sorted(scored, key=scored.get, reverse=True)
    return known + [h for h in DEFAULT_HOURS if h not in scored], scored


def hour_plan(state):
    """{local hour: posts} for today. Beyond 24 posts, the best hours get a second slot."""
    target = daily_target(state)
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


# --- sources ---------------------------------------------------------------

def score(insights):
    """Followers gained is what matters; fall back to shares+saves when unavailable."""
    if not insights:
        return (0, 0)
    primary = insights.get("follows")
    if primary is None:
        primary = insights.get("shares", 0) + insights.get("saved", 0)
    return (primary, insights.get("views", 0))


def shortcode(link):
    m = re.search(r"/(?:reel|reels|p)/([A-Za-z0-9_-]+)", link)
    return m.group(1) if m else link.strip()


def pick_sources(state):
    """Reels from sources.txt come first; the pool is topped up with the best-scoring reels
    so there is always enough to fill the daily target without repeating too soon."""
    originals = state["originals"]
    path = os.path.join(ROOT, "sources.txt")
    wanted = []
    if os.path.exists(path):
        wanted = [shortcode(l) for l in open(path, encoding="utf-8")
                  if l.strip() and not l.lstrip().startswith("#")]
    by_code = {shortcode(o["permalink"]): mid for mid, o in originals.items()}
    ids = [by_code[c] for c in wanted if c in by_code]
    missing = [c for c in wanted if c not in by_code]
    if missing:
        log(state, f"sources.txt: not found in account: {missing}")

    pool = max(CONFIG["min_pool"],
               math.ceil(daily_target(state) * CONFIG["min_days_between_reposts"] * CONFIG["pool_margin"]))
    retired = {mid for mid, s in state["sources"].items() if s["retired"] or s.get("broken")}
    ranked = sorted(
        (mid for mid, o in originals.items()
         if mid not in ids and mid not in retired
         and (o.get("insights") or {}).get("views", 0) >= CONFIG["min_views_for_auto"]),
        key=lambda mid: score(originals[mid].get("insights")), reverse=True)
    ids += ranked[:max(0, pool - len([i for i in ids if i not in retired]))]

    for rank, mid in enumerate(ids, 1):
        s = state["sources"].setdefault(mid, {"generation": 0, "retired": False, "last_posted": None})
        s["rank"] = rank
        s["active"] = True
    for mid, s in state["sources"].items():
        if mid not in ids:
            s["active"] = False


def next_sources(state):
    min_gap = CONFIG["min_days_between_reposts"] * 24
    candidates = [
        mid for mid, s in state["sources"].items()
        if s.get("active") and not s["retired"] and not s.get("broken")
        and hours_since(s["last_posted"]) >= min_gap
    ]
    # Never-posted first (by rank), then the one that waited longest.
    candidates.sort(key=lambda mid: (
        state["sources"][mid]["last_posted"] is not None,
        state["sources"][mid]["last_posted"] or "",
        state["sources"][mid].get("rank", 999),
    ))
    return candidates


# --- burn-out detection ----------------------------------------------------

def update_generations(state):
    for mid, s in state["sources"].items():
        if s["retired"]:
            continue
        gen_posts = [p for p in state["posts"]
                     if p["source"] == mid and p["generation"] == s["generation"]
                     and (p.get("insights") or {}).get("views") is not None]
        if not gen_posts:
            continue
        views = [p["insights"]["views"] for p in gen_posts]
        burned = (len(gen_posts) >= CONFIG["max_posts_per_generation"]
                  or (len(views) >= 2 and views[-1] < CONFIG["burn_ratio"] * max(views)))
        if burned:
            s["generation"] += 1
            if s["generation"] > CONFIG["max_generations"]:
                s["retired"] = True
                log(state, f"{mid} retired after {CONFIG['max_generations']} remix rounds")
            else:
                log(state, f"{mid} burned (views {views}) -> remix generation {s['generation']}")


# --- commands --------------------------------------------------------------

def cmd_sync(state):
    ig = get_client(state)
    posted_ids = {p["id"] for p in state["posts"]}
    for m in ig.list_reels():
        if m["id"] in posted_ids:
            continue
        o = state["originals"].setdefault(m["id"], {})
        o.update(caption=m.get("caption", ""), permalink=m["permalink"], timestamp=m["timestamp"])

    # Original reels: refresh stale insights, capped to stay inside API rate limits.
    budget = CONFIG["insights_calls_per_sync"]
    stale = sorted(state["originals"].items(), key=lambda kv: kv[1].get("insights_at") or "")
    for mid, o in stale:
        if budget <= 0:
            break
        if hours_since(o.get("insights_at")) < 24 * 14:
            continue
        try:
            o["insights"] = ig.insights(mid)
        except IGError as e:
            o["insights_error"] = str(e)[:200]
        o["insights_at"] = iso(now())
        budget -= 1

    # Trials: measure at 48h, then a final read after 7 days.
    for p in state["posts"]:
        age = hours_since(p["posted_at"])
        due = (age >= CONFIG["evaluate_after_hours"] and not p.get("insights")) or \
              (age >= 24 * 7 and not p.get("final"))
        if due:
            try:
                p["insights"] = ig.insights(p["id"])
                p["final"] = age >= 24 * 7
            except IGError as e:
                log(state, f"insights failed for trial {p['id']}: {e}")

    pick_sources(state)
    update_generations(state)
    state["last_sync"] = iso(now())
    log(state, f"sync done: {len(state['originals'])} reels, {len(state['posts'])} trials")


def post_one(state, ig):
    for mid in next_sources(state):
        s = state["sources"][mid]
        o = state["originals"][mid]
        try:
            url = ig.media_url(mid)
        except IGError as e:
            url = None
            log(state, f"{mid} media_url error: {e}")
        if not url:
            s["broken"] = True  # e.g. Instagram hides the file for licensed music
            log(state, f"{mid} has no downloadable video, skipping")
            continue

        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "src.mp4"), os.path.join(tmp, "out.mp4")
            with requests.get(url, stream=True, timeout=300) as r:
                r.raise_for_status()
                with open(src, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            info = remix(src, out, s["generation"], random.Random())
            if DRY_RUN:
                new_id = f"dry{len(state['posts'])}"
            else:
                new_id = ig.publish_trial_reel(out, o.get("caption", ""), CONFIG["graduation"])

        state["posts"].append({
            "id": new_id, "source": mid, "generation": s["generation"],
            "posted_at": iso(now()), "letter": info["letter"],
        })
        s["last_posted"] = iso(now())
        state.setdefault("started_at", iso(now()))
        log(state, f"posted trial {new_id} from {mid} (gen {s['generation']})")
        if CONFIG["notify_each_post"]:
            notify(state, f"✅ עלה טרייל\n{o['permalink']}\nסבב עריכה: {s['generation']}")
        return True
    return False


def cmd_run(state):
    if hours_since(state.get("last_sync")) >= CONFIG["sync_every_hours"]:
        cmd_sync(state)
    if CONFIG["enabled"]:
        owed = min(posts_owed(state), CONFIG["max_posts_per_run"])
        if owed:
            ig = get_client(state)
            for _ in range(owed):
                if not post_one(state, ig):
                    log(state, "nothing to post: all sources waiting or retired")
                    if not state.get("starved_warned") or hours_since(state["starved_warned"]) > 24:
                        notify(state, "⚠️ אין מספיק סרטונים במאגר כדי לעמוד ביעד היומי. "
                                      "אפשר להוסיף קישורים ל-sources.txt או להוריד את min_days_between_reposts.")
                        state["starved_warned"] = iso(now())
                    break
    if local().hour >= CONFIG["daily_report_hour"] and state.get("report_date") != str(local().date()):
        notify(state, daily_report(state))
        state["report_date"] = str(local().date())


# --- reporting -------------------------------------------------------------

def telegram_chat(state):
    """Chat id comes from the secret, or is discovered from the first message sent to the bot."""
    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot:
        return None, None
    chat = os.environ.get("TELEGRAM_CHAT_ID") or state.get("telegram_chat")
    if not chat:
        try:
            ups = requests.get(f"https://api.telegram.org/bot{bot}/getUpdates", timeout=20).json()
            chats = [u["message"]["chat"]["id"] for u in ups.get("result", []) if "message" in u]
            if chats:
                chat = state["telegram_chat"] = chats[-1]
        except requests.RequestException:
            pass
    return bot, chat


def notify(state, text):
    bot, chat = telegram_chat(state)
    if bot and chat and not DRY_RUN:
        try:
            requests.post(f"https://api.telegram.org/bot{bot}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=20)
        except requests.RequestException:
            pass


def fmt(n):
    return "—" if n is None else f"{n:,}"


def daily_report(state):
    posts = state["posts"]
    today = local().date()
    today_posts = [p for p in posts if local(p["posted_at"]).date() == today]
    measured = [p for p in posts if p.get("insights") and hours_since(p["posted_at"]) < 24 * 7]
    views = sum(p["insights"].get("views", 0) or 0 for p in measured)
    follows = sum(p["insights"].get("follows", 0) or 0 for p in measured)
    best = sorted(measured, key=lambda p: p["insights"].get("views", 0) or 0, reverse=True)[:3]
    plan = hour_plan(state)
    burned = sum(1 for s in state["sources"].values() if s.get("active") and s["generation"] > 0)
    lines = [
        f"📊 סיכום יומי {today:%d/%m}",
        f"עלו היום: {len(today_posts)} מתוך יעד {daily_target(state)}",
        f"7 ימים אחרונים (טריילים שנמדדו): {fmt(views)} צפיות, {fmt(follows)} עוקבים",
        f"שעות העלאה: {', '.join(f'{h:02d}' for h in sorted(plan))}",
        f"סרטונים במאגר: {sum(1 for s in state['sources'].values() if s.get('active'))} "
        f"(בעריכה: {burned})",
    ]
    if best:
        lines.append("🔥 הכי טובים:")
        for p in best:
            link = state["originals"].get(p["source"], {}).get("permalink", "")
            lines.append(f"• {fmt(p['insights'].get('views'))} צפיות — {link}")
    return "\n".join(lines)


def write_dashboard(state):
    posts = state["posts"]
    ranked, scored = ranked_hours(state)
    lines = [
        "# דשבורד טריילים", "",
        f"יעד יומי: {daily_target(state)} | תוכנית שעות: {hour_plan(state)}", "",
        "שעות לפי ביצועים: " + ", ".join(f"{h:02d} ({scored[h]:.2f})" for h in ranked if h in scored), "",
        "| # | סרטון | צפיות מקור | עוקבים מקור | סבב | טריילים | צפיות אחרון |",
        "|---|---|---|---|---|---|---|",
    ]
    for mid, s in sorted(state["sources"].items(), key=lambda kv: kv[1].get("rank", 999)):
        if not s.get("active"):
            continue
        o = state["originals"].get(mid, {})
        ins = o.get("insights") or {}
        mine = [p for p in posts if p["source"] == mid]
        last = next((p for p in reversed(mine) if p.get("insights")), None)
        lines.append(f"| {s.get('rank')} | {o.get('permalink')} | {fmt(ins.get('views'))} | "
                     f"{fmt(ins.get('follows'))} | {s['generation']} | {len(mine)} | "
                     f"{fmt((last or {}).get('insights', {}).get('views'))} |")
    lines += ["", "```", *state["log"][-30:], "```"]
    with open(os.path.join(ROOT, "DASHBOARD.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("run", "sync", "report"):
        sys.exit(__doc__)
    state = load_state()
    if cmd == "report":
        write_dashboard(state)
        print(daily_report(state))
        return
    try:
        cmd_sync(state) if cmd == "sync" else cmd_run(state)
    except Exception as e:
        log(state, f"ERROR in {cmd}: {e}")
        notify(state, f"❌ שגיאה בבוט: {e}")
        save_state(state)
        raise
    save_state(state)


if __name__ == "__main__":
    main()
