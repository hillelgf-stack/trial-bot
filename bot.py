"""Trial-reel bot.

    python bot.py sync   pull reels + insights, evaluate past trials, mark burned videos
    python bot.py post   repost the next winning reel as a Trial Reel

State lives in data/state.json and is committed back to the repo by the workflow.
"""
import base64
import hashlib
import json
import os
import random
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import requests
from cryptography.fernet import Fernet, InvalidToken

from ig import Instagram, IGError
from remix import remix

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "data", "state.json")
TOKEN_PATH = os.path.join(ROOT, "data", "token.enc")
CONFIG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts):
    return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def hours_since(ts):
    return (now() - parse(ts)).total_seconds() / 3600 if ts else float("inf")


# --- state -----------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_PATH):
        return json.load(open(STATE_PATH, encoding="utf-8"))
    return {"originals": {}, "sources": {}, "posts": [], "log": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state["log"] = state["log"][-200:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    write_dashboard(state)


def log(state, msg):
    line = f"{iso(now())} {msg}"
    print(line)
    state["log"].append(line)


# --- token: kept encrypted in the repo so the bot can refresh it by itself ---

def _fernet():
    seed = os.environ["IG_TOKEN"].encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed).digest()))


def get_client(state):
    token = os.environ["IG_TOKEN"]
    if os.path.exists(TOKEN_PATH):
        try:
            token = _fernet().decrypt(open(TOKEN_PATH, "rb").read()).decode()
        except InvalidToken:
            pass  # IG_TOKEN secret was replaced; start from it
    ig = Instagram(token, CONFIG["api_version"])
    if hours_since(state.get("token_refreshed_at")) > 24 * 7 and not DRY_RUN:
        try:
            token = ig.refresh_token()
            os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
            open(TOKEN_PATH, "wb").write(_fernet().encrypt(token.encode()))
            state["token_refreshed_at"] = iso(now())
            log(state, "token refreshed")
        except IGError as e:
            log(state, f"token refresh failed: {e}")
            notify(f"⚠️ חידוש הטוקן נכשל: {e}")
    return ig


# --- ranking / selection ---------------------------------------------------

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
    """Manual list in sources.txt wins; otherwise the top N reels by score."""
    path = os.path.join(ROOT, "sources.txt")
    wanted = []
    if os.path.exists(path):
        wanted = [shortcode(l) for l in open(path, encoding="utf-8")
                  if l.strip() and not l.lstrip().startswith("#")]
    originals = state["originals"]
    if wanted:
        by_code = {shortcode(o["permalink"]): mid for mid, o in originals.items()}
        ids = [by_code[c] for c in wanted if c in by_code]
        missing = [c for c in wanted if c not in by_code]
        if missing:
            log(state, f"sources.txt: not found in account: {missing}")
    else:
        ranked = sorted(
            (mid for mid, o in originals.items()
             if (o.get("insights") or {}).get("views", 0) >= CONFIG["min_views_for_auto"]),
            key=lambda mid: score(originals[mid].get("insights")), reverse=True)
        ids = ranked[:CONFIG["auto_top_n"]]
    for rank, mid in enumerate(ids, 1):
        s = state["sources"].setdefault(mid, {"generation": 0, "retired": False, "last_posted": None})
        s["rank"] = rank
        s["active"] = True
    for mid, s in state["sources"].items():
        if mid not in ids:
            s["active"] = False


def next_source(state):
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
    reels = [m for m in ig.list_reels() if m["id"] not in posted_ids]
    for m in reels:
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


def cmd_post(state):
    if not CONFIG["enabled"]:
        print("disabled in config.json")
        return
    if not state["originals"]:
        cmd_sync(state)
    ig = get_client(state)
    for mid in next_source(state):
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
                log(state, f"[dry run] would post {mid} gen {s['generation']} letter {info['letter']}")
                return
            new_id = ig.publish_trial_reel(out, o.get("caption", ""), CONFIG["graduation"])

        state["posts"].append({
            "id": new_id, "source": mid, "generation": s["generation"],
            "posted_at": iso(now()), "letter": info["letter"],
        })
        s["last_posted"] = iso(now())
        log(state, f"posted trial {new_id} from {mid} (gen {s['generation']})")
        notify(f"✅ עלה טרייל חדש\nמקור: {o['permalink']}\nסבב עריכה: {s['generation']}")
        return
    log(state, "nothing to post (all sources waiting / retired)")
    notify("ℹ️ אין כרגע סרטון זמין להעלאה — כולם בהמתנה בין העלאות או נשחקו.")


# --- reporting -------------------------------------------------------------

def notify(text):
    bot, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if bot and chat and not DRY_RUN:
        try:
            requests.post(f"https://api.telegram.org/bot{bot}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=20)
        except requests.RequestException:
            pass


def fmt(n):
    return "—" if n is None else f"{n:,}"


def write_dashboard(state):
    posts = state["posts"]
    week = [p for p in posts if hours_since(p["posted_at"]) < 24 * 7]
    lines = [
        "# 📊 דשבורד טריילים", "",
        f"עדכון אחרון: {state.get('last_sync', '—')} (UTC)", "",
        f"- **טריילים שעלו:** {len(posts)} (השבוע: {len(week)})",
        f"- **עוקבים מטריילים:** {fmt(sum((p.get('insights') or {}).get('follows', 0) or 0 for p in posts))}",
        f"- **צפיות מטריילים:** {fmt(sum((p.get('insights') or {}).get('views', 0) or 0 for p in posts))}",
        "", "## סרטונים במחזור", "",
        "| # | סרטון | צפיות מקור | עוקבים מקור | סבב עריכה | טריילים | צפיות טרייל אחרון | מצב |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for mid, s in sorted(state["sources"].items(), key=lambda kv: kv[1].get("rank", 999)):
        if not s.get("active"):
            continue
        o = state["originals"].get(mid, {})
        ins = o.get("insights") or {}
        mine = [p for p in posts if p["source"] == mid]
        last = next((p for p in reversed(mine) if p.get("insights")), None)
        status = "🔴 פרש" if s["retired"] else "⚠️ לא ניתן להורדה" if s.get("broken") else \
            "🟢 מקורי" if s["generation"] == 0 else f"🟡 ערוך ×{s['generation']}"
        lines.append(
            f"| {s.get('rank', '')} | [קישור]({o.get('permalink', '')}) | {fmt(ins.get('views'))} | "
            f"{fmt(ins.get('follows'))} | {s['generation']} | {len(mine)} | "
            f"{fmt((last or {}).get('insights', {}).get('views'))} | {status} |")
    lines += ["", "## טריילים אחרונים", "", "| מתי | מקור | סבב | צפיות | עוקבים |", "|---|---|---|---|---|"]
    for p in reversed(posts[-30:]):
        ins = p.get("insights") or {}
        link = state["originals"].get(p["source"], {}).get("permalink", "")
        lines.append(f"| {p['posted_at'][:16].replace('T', ' ')} | [מקור]({link}) | {p['generation']} | "
                     f"{fmt(ins.get('views'))} | {fmt(ins.get('follows'))} |")
    lines += ["", "## לוג", "", "```", *state["log"][-15:], "```"]
    with open(os.path.join(ROOT, "DASHBOARD.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("sync", "post"):
        sys.exit(__doc__)
    state = load_state()
    try:
        cmd_sync(state) if cmd == "sync" else cmd_post(state)
    except Exception as e:
        log(state, f"ERROR in {cmd}: {e}")
        notify(f"❌ שגיאה ב-{cmd}: {e}")
        save_state(state)
        raise
    save_state(state)


if __name__ == "__main__":
    main()
