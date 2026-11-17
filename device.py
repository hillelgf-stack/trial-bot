"""Bring the posting phone up only while posting.

With a real Android phone plugged in (USB debugging on), it is used as-is.
Otherwise the virtual phone is booted headless, heavy video-editing work is
paused for the few minutes of posting, and everything is restored afterwards.
"""
import contextlib
import os
import subprocess
import time

import psutil

SDK = os.path.expanduser(r"~\android\sdk")
ADB = os.path.join(SDK, "platform-tools", "adb.exe")
EMULATOR = os.path.join(SDK, "emulator", "emulator.exe")
EMU_SERIAL = "emulator-5554"

# processes from the video-editing work that are paused while the virtual phone runs
HEAVY_NAMES = {"ffmpeg.exe", "ffprobe.exe"}
HEAVY_CMD_HINTS = ("video-edit", "transcribe.py", "matte.py")


def _adb(*args, timeout=60):
    return subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)


def real_phone_serial():
    """Serial of a plugged-in real Android phone, or None."""
    out = _adb("devices").stdout.splitlines()[1:]
    for line in out:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device" and not parts[0].startswith("emulator-"):
            return parts[0]
    return None


def _heavy_processes():
    me = os.getpid()
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if p.pid == me:
                continue
            name = (p.info["name"] or "").lower()
            cmd = " ".join(p.info["cmdline"] or [])
            if name in HEAVY_NAMES or (name.startswith("python") and any(h in cmd for h in HEAVY_CMD_HINTS)):
                yield p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


@contextlib.contextmanager
def paused_editing(log=print):
    paused = []
    for p in _heavy_processes():
        try:
            p.suspend()
            paused.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if paused:
        log(f"paused {len(paused)} editing process(es)")
    try:
        yield
    finally:
        for p in paused:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                p.resume()
        if paused:
            log("resumed editing")


def _emulator_running():
    return any((p.info["name"] or "").lower().startswith("qemu-system") for p in psutil.process_iter(["name"]))


def start_emulator(log=print, boot_timeout=420):
    if not _emulator_running():
        for lock in ("hardware-qemu.ini.lock", "multiinstance.lock"):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(os.path.expanduser(r"~\.android\avd\trialbot.avd"), lock))
        env = dict(os.environ, ANDROID_HOME=SDK, ANDROID_SDK_ROOT=SDK)
        subprocess.Popen([EMULATOR, "-avd", "trialbot", "-no-window", "-gpu", "swiftshader_indirect",
                          "-no-boot-anim", "-no-snapshot", "-no-audio", "-dns-server", "8.8.8.8,1.1.1.1"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)
        log("virtual phone starting")
    end = time.time() + boot_timeout
    while time.time() < end:
        r = _adb("-s", EMU_SERIAL, "shell", "getprop", "sys.boot_completed", timeout=20)
        if r.stdout.strip() == "1":
            for k in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
                _adb("-s", EMU_SERIAL, "shell", "settings", "put", "global", k, "0")
            time.sleep(20)  # let Play services and Instagram settle after boot
            return EMU_SERIAL
        time.sleep(5)
    raise RuntimeError("virtual phone did not boot in time")


def stop_emulator(log=print):
    _adb("-s", EMU_SERIAL, "emu", "kill", timeout=30)
    for _ in range(20):
        if not _emulator_running():
            break
        time.sleep(2)
    else:
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").lower().startswith("qemu-system"):
                with contextlib.suppress(psutil.Error):
                    p.kill()
    log("virtual phone off")


@contextlib.contextmanager
def posting_phone(log=print):
    """Yield the adb serial of a phone ready for posting."""
    serial = real_phone_serial()
    if serial:
        log(f"using real phone {serial}")
        yield serial
        return
    with paused_editing(log):
        started_here = not _emulator_running()
        serial = start_emulator(log)
        try:
            yield serial
        finally:
            if started_here:
                stop_emulator(log)
