"""Open the robot's Instagram browser (persistent profile) and keep it open.

Other scripts attach to it on http://127.0.0.1:9333 (CDP).
"""
import os
import time

from playwright.sync_api import sync_playwright

PROFILE = os.path.join(os.path.expanduser("~"), "trial-bot-profile")
PORT = 9333

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        PROFILE,
        headless=False,
        channel="msedge",
        viewport=None,
        locale="he-IL",
        args=[f"--remote-debugging-port={PORT}", "--start-maximized"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://www.instagram.com/")
    page.bring_to_front()
    # stay up until the browser itself goes away (tabs may come and go)
    ctx.wait_for_event("close", timeout=0)

