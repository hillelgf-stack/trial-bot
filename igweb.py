"""Read the user's reels from instagram.com in the robot's own Edge profile (already logged in).

Only browses like a person (opens pages, scrolls) and reads the data those pages load.
Use it as:

    with igweb.Web() as web:
        reels = web.list_reels("hillelgf")
        det = web.reel_details(code)
"""
import contextlib
import json
import os
import random
import time

import psutil
import requests
from playwright.sync_api import sync_playwright

PROFILE = os.path.join(os.path.expanduser("~"), "trial-bot-profile")


def _walk(o, fn):
    if isinstance(o, dict):
        fn(o)
        for v in o.values():
            _walk(v, fn)
    elif isinstance(o, list):
        for v in o:
            _walk(v, fn)


def _close_stray_profile_windows():
    """A leftover robot window would lock the profile; close only those."""
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() == "msedge.exe" and PROFILE in " ".join(p.info["cmdline"] or []):
                p.kill()
        except psutil.Error:
            pass
    time.sleep(2)


class Web:
    def __enter__(self):
        _close_stray_profile_windows()
        self._pw = sync_playwright().start()
        for attempt in range(3):
            try:
                self.ctx = self._pw.chromium.launch_persistent_context(
                    PROFILE, channel="msedge", headless=False, viewport={"width": 1280, "height": 900},
                    locale="he-IL", args=["--window-position=-2400,0"])  # off-screen, out of the user's way
                break
            except Exception:
                if attempt == 2:
                    self._pw.stop()
                    raise
                time.sleep(10)  # a just-closed window can hold the profile for a few seconds
                _close_stray_profile_windows()
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self._keep = None
        self.page.on("response", self._on_response)
        return self

    def __exit__(self, *exc):
        with contextlib.suppress(Exception):
            self.ctx.close()
        with contextlib.suppress(Exception):
            self._pw.stop()

    def _on_response(self, resp):
        if self._keep is None or "instagram.com" not in resp.url:
            return
        if "graphql" not in resp.url and "/api/v1/" not in resp.url:
            return
        try:
            data = resp.json()
        except Exception:
            return
        keep = self._keep
        _walk(data, lambda o: keep(o) if o.get("code") and "media_type" in o else None)

    def logged_in(self):
        self.page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        time.sleep(5)
        return "accounts/login" not in self.page.url and self.page.locator("input[name='password']").count() == 0

    def list_reels(self, username, max_scrolls=60, stop_after_known=None):
        """Return {code: {"plays", "likes", "comments"}} by scrolling the Reels tab.

        stop_after_known: a set of codes; scrolling stops once a page adds only known codes.
        """
        reels = {}

        def keep(o):
            if o.get("product_type") == "clips" and o.get("play_count") is not None:
                reels[o["code"]] = {"plays": o.get("play_count"), "likes": o.get("like_count"),
                                    "comments": o.get("comment_count")}

        self._keep = keep
        try:
            self.page.goto(f"https://www.instagram.com/{username}/reels/", wait_until="domcontentloaded")
            time.sleep(random.uniform(7, 10))
            stale = 0
            for _ in range(max_scrolls):
                before = len(reels)
                self.page.mouse.wheel(0, random.randint(1800, 2600))
                time.sleep(random.uniform(3.5, 6.5))
                new = set(list(reels)[before:])
                if len(reels) == before:
                    stale += 1
                    if stale >= 4:
                        break
                else:
                    stale = 0
                    if stop_after_known is not None and new <= stop_after_known:
                        break
        finally:
            self._keep = None
        return reels

    def reel_details(self, code):
        """Open one reel's page and return {"caption", "taken_at", "video_url", "duration"}."""
        found = {}

        def keep(o):
            if o.get("code") == code and o.get("video_versions"):
                found.update(o)

        self._keep = keep
        try:
            self.page.goto(f"https://www.instagram.com/reel/{code}/", wait_until="domcontentloaded")
            for _ in range(20):
                if found:
                    break
                time.sleep(1)
            if not found:
                # the first load is often embedded in the HTML instead of a separate request
                for script in self.page.locator("script[type='application/json']").all():
                    with contextlib.suppress(Exception):
                        _walk(json.loads(script.text_content() or "null"), keep)
                    if found:
                        break
        finally:
            self._keep = None
        if not found:
            return None
        best = max(found["video_versions"], key=lambda v: (v.get("width") or 0) * (v.get("height") or 0))
        return {"caption": ((found.get("caption") or {}).get("text") or ""),
                "taken_at": found.get("taken_at"),
                "video_url": best["url"],
                "duration": found.get("video_duration")}


def download(url, path):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return path
