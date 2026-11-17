"""Drive the Instagram app on an Android phone (real or virtual) to post a Trial Reel."""
import os
import shutil
import subprocess
import time

ADB_DIR = os.path.expanduser(r"~\android\sdk\platform-tools")
os.environ["PATH"] = ADB_DIR + os.pathsep + os.environ["PATH"]
import uiautomator2 as u2  # noqa: E402

SERIAL = "emulator-5554"
IG = "com.instagram.android"
PHONE_DIR = "/sdcard/DCIM/TrialBot"
ADB = os.path.join(ADB_DIR, "adb.exe")

NEXT = r"(?i)^(next|הבא)$"
SHARE = r"(?i)^(share|שיתוף)$"
OK_PROMPTS = r"(?i)^(ok|got it|continue|הבנתי|אישור|המשך)$"


class PhoneError(Exception):
    pass


def use(serial):
    """Point every following call at this phone (real or virtual)."""
    global SERIAL
    SERIAL = serial


def adb(*args, timeout=120):
    r = subprocess.run([ADB, "-s", SERIAL, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise PhoneError(f"adb {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def push_video(local_path, name):
    """Copy a video into the phone gallery; returns its path on the phone."""
    adb("shell", "mkdir", "-p", PHONE_DIR)
    remote = f"{PHONE_DIR}/{name}.mp4"
    if len(os.path.abspath(local_path)) > 200:  # adb can't read paths past the Windows 260-char limit
        short = os.path.join(os.path.expanduser("~"), "trial-bot-push.mp4")
        shutil.copyfile(local_path, short)
        local_path = short
    adb("push", local_path, remote, timeout=600)
    adb("shell", "touch", remote)
    adb("shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", f"file://{remote}")
    time.sleep(5)
    return remote


def clear_pushed():
    adb("shell", "rm", "-f", f"{PHONE_DIR}/*.mp4")
    adb("shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", f"file://{PHONE_DIR}")


class Poster:
    def __init__(self, log=print):
        self.d = u2.connect(SERIAL)
        self.d.implicitly_wait(15)
        self.log = log

    # -- helpers -------------------------------------------------------
    def _dismiss(self):
        """Answer the system 'not responding' and harmless one-time prompts."""
        d = self.d
        if d(text="Wait").exists:
            d(text="Wait").click()
            time.sleep(3)
        # the caption editor's own "OK" lives in the action bar; leave that one alone
        p = d(textMatches=OK_PROMPTS, resourceIdMatches=r"^(?!.*action_bar_button_text).*$")
        if p.exists:
            self.log(f"prompt: {p.get_text()}")
            p.click()
            time.sleep(2)

    def _wait_for(self, timeout, **sel):
        end = time.time() + timeout
        while time.time() < end:
            self._dismiss()
            if self.d(**sel).exists:
                return self.d(**sel)
            time.sleep(2)
        raise PhoneError(f"timed out waiting for {sel}")

    def _snap(self, tag, folder):
        if folder:
            os.makedirs(folder, exist_ok=True)
            self.d.screenshot(os.path.join(folder, f"{tag}.png"))

    # -- flow ----------------------------------------------------------
    def open_reel_composer(self):
        d = self.d
        d.app_stop(IG)
        d.app_start(IG)
        self._wait_for(180, resourceId=f"{IG}:id/feed_tab")
        time.sleep(3)
        w, h = d.window_size()
        d.click(int(w * 0.067), int(h * 0.115))  # the "+" at the top-left of the home feed
        end = time.time() + 120
        while time.time() < end:
            self._dismiss()
            if d(text="Start new video").exists:
                d(text="Start new video").click()
            if d(text="REEL").exists or d(text="New reel").exists:
                break
            time.sleep(2)
        else:
            raise PhoneError("create screen did not open")
        if not d(text="New reel").exists:
            d(text="REEL").click()
        self._wait_for(30, text="New reel")

    def pick_newest_video(self):
        tiles = self.d(descriptionStartsWith="Unselected Video thumbnail")
        if not tiles.exists:
            raise PhoneError("no video in the gallery")
        tiles[0].click()
        self._wait_for(90, textMatches=NEXT).click()

    def fill_caption_and_trial(self, caption):
        d = self.d
        field = self._wait_for(60, textStartsWith="Write a caption")
        field.click()
        time.sleep(2)
        box = d(resourceId=f"{IG}:id/caption_input_text_view")
        box.set_text(caption)
        time.sleep(2)
        typed = box.get_text() or ""
        if caption.strip() and typed.strip()[:20] != caption.strip()[:20]:
            raise PhoneError("caption did not paste correctly")
        done = d(resourceId=f"{IG}:id/action_bar_button_text", text="OK")
        if done.exists:
            done.click()
        else:
            d.press("back")  # close the keyboard
        time.sleep(2)
        if not d(textStartsWith="Tag people").exists:
            raise PhoneError("left the caption screen while closing the keyboard")

        d(scrollable=True).scroll.to(text="Trial")
        row = d(resourceId=f"{IG}:id/title", text="Trial")
        if not row.exists:
            raise PhoneError("Trial option not found")
        toggle = row.sibling(resourceId=f"{IG}:id/toggle")
        if not toggle.exists:
            toggle = row.right(resourceId=f"{IG}:id/toggle")
        if not toggle.info.get("checked"):
            toggle.click()
            time.sleep(2)
            self._dismiss()
        toggle = d(resourceId=f"{IG}:id/title", text="Trial").right(resourceId=f"{IG}:id/toggle")
        if not (toggle and toggle.info.get("checked")):
            raise PhoneError("Trial switch did not turn on")
        # never cross-post from here
        for name in ("Threads", "Facebook"):
            if not d(text=name).exists:  # these rows go away once Trial is on
                continue
            t = d(text=name).right(resourceId=f"{IG}:id/toggle")
            if t and t.exists and t.info.get("checked"):
                t.click()
                time.sleep(1)

    def share(self):
        d = self.d
        # with Trial on, the caption screen's button already reads "Share"
        first = d(textMatches=SHARE) if d(textMatches=SHARE).exists else d(textMatches=NEXT)
        first.click()
        end = time.time() + 60
        while time.time() < end:
            self._dismiss()
            btn = d(textMatches=SHARE)
            if btn.exists:
                btn.click()
                break
            if d(resourceId=f"{IG}:id/feed_tab").exists:
                break
            time.sleep(2)
        else:
            raise PhoneError("no Share step after Next")
        self._wait_for(180, resourceId=f"{IG}:id/feed_tab")

    def post_trial(self, caption, dry_run=False, snaps=None, attempts=3):
        # Right after a cold boot Instagram sometimes hasn't loaded the Trial option yet;
        # reopen and try again rather than ever posting without it.
        for attempt in range(1, attempts + 1):
            self.open_reel_composer()
            self._snap("1_composer", snaps)
            self.pick_newest_video()
            self._snap("2_editor", snaps)
            try:
                self.fill_caption_and_trial(caption)
                break
            except PhoneError as e:
                if "Trial" not in str(e) or attempt == attempts:
                    raise
                self.log(f"{e}; reopening Instagram (try {attempt + 1}/{attempts})")
                time.sleep(30)
        self._snap("3_ready", snaps)
        if dry_run:
            self.log("dry run: stopping before Next/Share")
            self.d.press("back")
            return False
        self.share()
        self._snap("4_after", snaps)
        return True
