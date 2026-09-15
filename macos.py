"""
macOS plumbing for David — everything that touches the operating system itself.

Uses only what ships with every Mac: ctypes into CoreGraphics and the Mach
kernel, plus the built-in command-line tools (open, osascript, screencapture,
sips, pbcopy, pmset, say). No extra packages.

Two permissions matter, and both are granted to whichever app runs David
(Terminal, iTerm, VS Code), in System Settings -> Privacy & Security:
  Screen Recording  — reading the screen
  Accessibility     — clicking, typing, pressing keys, moving windows
Without them macOS does not refuse loudly: a capture comes back as bare
wallpaper and a click simply never lands. So every caller checks
can_see_screen() / can_control() first and says so, rather than reporting
success on something that did not happen.
"""

import ctypes
import ctypes.util
import json
import os
import re
import shutil
import subprocess
import threading
import time

_AS = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_LIBC = ctypes.CDLL(ctypes.util.find_library("c"))

NO_CONTROL = ("macOS has not given me Accessibility permission, so I cannot click, type or move "
              "windows. Turn it on for the app I am running in, under System Settings, Privacy and "
              "Security, Accessibility, then restart me")
NO_SCREEN = ("macOS has not given me Screen Recording permission, so I cannot see the screen. "
             "Turn it on for the app I am running in, under System Settings, Privacy and Security, "
             "Screen Recording, then restart me")
NO_MIC = ("the microphone is sending complete silence. Either macOS Microphone permission is off "
          "for the app I am running in — System Settings, Privacy and Security, Microphone — "
          "or the microphone is muted")


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _Size(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _Rect(ctypes.Structure):
    _fields_ = [("origin", _Point), ("size", _Size)]


_AS.AXIsProcessTrusted.restype = ctypes.c_bool
_AS.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
_AS.CGDisplayBounds.restype = _Rect
_AS.CGDisplayBounds.argtypes = [ctypes.c_uint32]
_AS.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
_AS.CGEventCreateMouseEvent.restype = ctypes.c_void_p
_AS.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, _Point, ctypes.c_uint32]
_AS.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
_AS.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
_AS.CGEventCreateScrollWheelEvent2.restype = ctypes.c_void_p
_AS.CGEventCreateScrollWheelEvent2.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                               ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
_AS.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_AS.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
_AS.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_CF.CFRelease.argtypes = [ctypes.c_void_p]
_LIBC.mach_host_self.restype = ctypes.c_uint32
_LIBC.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                               ctypes.c_void_p, ctypes.c_size_t]

_HOST = _LIBC.mach_host_self()


# ---- permissions ----

def can_control():
    return bool(_AS.AXIsProcessTrusted())


def can_see_screen():
    return bool(_AS.CGPreflightScreenCaptureAccess())


# ---- running things ----

def _run(args, timeout=10, text_in=None):
    """Run a command and return (ok, stdout). Never raises — a missing tool or
       a hung one is just a failure the caller can report."""
    try:
        out = subprocess.run(args, input=text_in, capture_output=True, text=True, timeout=timeout,
                             env={**os.environ, "LANG": "en_US.UTF-8"})
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return out.returncode == 0, out.stdout.strip()


def _q(text):
    """An AppleScript string literal."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def osascript(script, timeout=10):
    return _run(["osascript", "-e", script], timeout=timeout)


def open_target(target, app=None, extra_args=None):
    """Open a file, folder, web address or app:// link — in a named app if given.
       extra_args go to the app itself (Chromium's --new-window, --app=...)."""
    if app and extra_args:
        cmd = ["open", "-na", app, "--args", *extra_args, target]
    elif app:
        cmd = ["open", "-a", app, target]
    else:
        cmd = ["open", target]
    return _run(cmd, timeout=15)[0]


def launch_app(app):
    return _run(["open", "-a", app], timeout=15)[0]


# Browsers that share Chrome's AppleScript dictionary (windows, tabs, URL).
CHROMIUM = {"Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"}


def browser_tab_script(app, url, match):
    """AppleScript that shows `url` in the first tab whose address contains
       `match`, or in a new tab when there is none. Returns true when it reused one."""
    return (
        f"tell application {_q(app)}\n"
        "  set found to false\n"
        "  repeat with w in windows\n"
        "    set i to 0\n"
        "    repeat with t in tabs of w\n"
        "      set i to i + 1\n"
        f"      if URL of t contains {_q(match)} then\n"
        f"        set URL of t to {_q(url)}\n"
        "        set active tab index of w to i\n"
        "        set index of w to 1\n"
        "        set found to true\n"
        "        exit repeat\n"
        "      end if\n"
        "    end repeat\n"
        "    if found then exit repeat\n"
        "  end repeat\n"
        "  if not found then\n"
        "    if (count of windows) is 0 then\n"
        "      make new window\n"
        f"      set URL of active tab of window 1 to {_q(url)}\n"
        "    else\n"
        f"      tell window 1 to make new tab with properties {{URL:{_q(url)}}}\n"
        "    end if\n"
        "  end if\n"
        "  activate\n"
        "  return found\n"
        "end tell")


def show_in_browser_tab(app, url, match):
    """Reuse the tab already on a site instead of taking over whichever tab is in
       front. True if a tab was reused, False if a new one opened, None if the
       browser can't be scripted (Safari, or Automation permission refused).
       The long timeout covers macOS asking for Automation permission the first time."""
    if app not in CHROMIUM:
        return None
    ok, out = osascript(browser_tab_script(app, url, match), timeout=30)
    return (out == "true") if ok else None


def open_app_window(app, url):
    """A Chromium browser's frameless app window — how the HUD gets a window of its own."""
    return _run(["open", "-na", app, "--args", f"--app={url}"], timeout=15)[0]


# ---- installed apps ----

_APP_ROOTS = ["/Applications", "/Applications/Utilities", "/System/Applications",
              "/System/Applications/Utilities", os.path.expanduser("~/Applications")]


def installed_apps():
    """Every .app in the usual places, one folder deep (Microsoft Office and
       friends keep theirs in a subfolder). Name, lowercased -> path."""
    found = {"finder": "/System/Library/CoreServices/Finder.app"}
    for root in _APP_ROOTS:
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(root, entry)
            if entry.endswith(".app"):
                found.setdefault(entry[:-4].lower(), path)
            elif os.path.isdir(path) and not entry.startswith("."):
                try:
                    inner = os.listdir(path)
                except OSError:
                    continue
                for sub in inner:
                    if sub.endswith(".app"):
                        found.setdefault(sub[:-4].lower(), os.path.join(path, sub))
    return found


def app_name(path):
    return os.path.basename(path)[:-4] if path.endswith(".app") else os.path.basename(path)


# ---- keyboard and mouse ----

_KEYS = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "backspace": 51, "escape": 53, "esc": 53,
    "delete": 117, "left": 123, "right": 124, "down": 125, "up": 126, "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121, "f5": 96, "f11": 103,
    "a": 0, "c": 8, "f": 3, "h": 4, "l": 37, "m": 46, "q": 12, "r": 15, "t": 17, "v": 9, "w": 13,
}
_MODS = {"cmd": 0x100000, "shift": 0x20000, "ctrl": 0x40000, "option": 0x80000}


def has_key(name):
    return name in _KEYS


def press(key, *mods):
    """Press one key with any modifiers held — press("v", "cmd") pastes."""
    flags = 0
    for m in mods:
        flags |= _MODS[m]
    for down in (True, False):
        ev = _AS.CGEventCreateKeyboardEvent(None, _KEYS[key], down)
        _AS.CGEventSetFlags(ev, flags)
        _AS.CGEventPost(0, ev)
        _CF.CFRelease(ev)
        time.sleep(0.01)


def _mouse(kind, point, clicks=1):
    ev = _AS.CGEventCreateMouseEvent(None, kind, point, 0)
    _AS.CGEventSetIntegerValueField(ev, 1, clicks)     # kCGMouseEventClickState
    _AS.CGEventPost(0, ev)
    _CF.CFRelease(ev)


def click(x, y, double=False):
    point = _Point(float(x), float(y))
    _mouse(5, point)                                   # move there first, so hover states fire
    time.sleep(0.08)
    for n in ((1, 2) if double else (1,)):
        _mouse(1, point, n)                            # left down
        _mouse(2, point, n)                            # left up
        time.sleep(0.06)


def scroll_lines(lines):
    """Positive scrolls up, negative down."""
    ev = _AS.CGEventCreateScrollWheelEvent2(None, 1, 1, int(lines), 0, 0)
    _AS.CGEventPost(0, ev)
    _CF.CFRelease(ev)


# ---- clipboard ----

def set_clipboard(text):
    return _run(["pbcopy"], text_in=text)[0]


def get_clipboard():
    return _run(["pbpaste"])[1]


def image_to_clipboard(path):
    kind = "«class PNGf»" if path.lower().endswith(".png") else "JPEG picture"
    return osascript(f"set the clipboard to (read (POSIX file {_q(path)}) as {kind})")[0]


# ---- screens and windows ----

def displays():
    """Every screen attached, left to right, in the same point coordinates
       that clicks use — (0, 0) is the top-left of the main screen."""
    ids = (ctypes.c_uint32 * 16)()
    count = ctypes.c_uint32()
    if _AS.CGGetActiveDisplayList(16, ids, ctypes.byref(count)) != 0:
        return []
    rects = []
    for i in range(count.value):
        r = _AS.CGDisplayBounds(ids[i])
        rects.append({"left": int(r.origin.x), "top": int(r.origin.y),
                      "right": int(r.origin.x + r.size.width),
                      "bottom": int(r.origin.y + r.size.height),
                      "builtin": bool(_AS.CGDisplayIsBuiltin(ids[i]))})
    rects.sort(key=lambda r: r["left"])
    return rects


def external_display_names():
    """Hardware names of plugged-in screens, as System Information lists them.
       The built-in screen is left out — it has no useful name."""
    ok, out = _run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=20)
    names = []
    try:
        for gpu in json.loads(out).get("SPDisplaysDataType", []):
            for d in gpu.get("spdisplays_ndrvs", []):
                internal = d.get("spdisplays_connection_type") == "spdisplays_internal"
                if not internal and d.get("_name") not in (None, "Color LCD"):
                    names.append(d["_name"])
    except (ValueError, AttributeError):
        pass
    return names


def front_app():
    ok, out = osascript('tell application "System Events" to get name of first application '
                        'process whose frontmost is true')
    return out if ok else ""


def app_running(name):
    ok, out = osascript(f'tell application "System Events" to (name of application processes) '
                        f'contains {_q(name)}')
    return ok and out == "true"


def activate_app(name):
    ok, _ = osascript(f'tell application "System Events" to set frontmost of first application '
                      f'process whose name is {_q(name)} to true')
    return ok


def set_front_window(left, top, width, height):
    script = (
        'tell application "System Events"\n'
        '  tell (first application process whose frontmost is true)\n'
        f'    set position of window 1 to {{{int(left)}, {int(top)}}}\n'
        f'    set size of window 1 to {{{int(width)}, {int(height)}}}\n'
        '  end tell\n'
        'end tell')
    return osascript(script)[0]


def hide_all_apps():
    return osascript('tell application "System Events" to set visible of (every application '
                     'process whose visible is true and name is not "Finder") to false')[0]


def quit_app(name):
    return osascript(f"tell application {_q(name)} to quit", timeout=15)[0]


def capture(path, rect=None, max_width=None, fmt="png"):
    """Screenshot to a file — one screen's rectangle, or the main screen —
       shrunk to max_width pixels so a Retina grab doesn't blow the quota."""
    if not can_see_screen():
        return False
    cmd = ["screencapture", "-x", "-t", fmt]
    if rect:
        left, top, right, bottom = rect
        cmd += ["-R", f"{left},{top},{right - left},{bottom - top}"]
    cmd.append(path)
    if not _run(cmd, timeout=15)[0] or not os.path.exists(path):
        return False
    if max_width:
        ok, out = _run(["sips", "-g", "pixelWidth", path])
        m = re.search(r"pixelWidth:\s*(\d+)", out)
        if ok and m and int(m.group(1)) > int(max_width):
            _run(["sips", "--resampleWidth", str(int(max_width)), path], timeout=15)
    return True


# ---- sound ----

def change_volume(direction):
    if direction == "mute":
        script = "set volume output muted not (output muted of (get volume settings))"
    else:
        step = 12 if direction != "down" else -12
        script = f"set volume output volume ((output volume of (get volume settings)) + {step})"
    return osascript(script)[0]


def speak(text, rate=210):
    _run(["say", "-r", str(rate), text], timeout=15)


def media_control(command):
    """Play, pause, next or previous in whichever of Spotify or Music is open.
       Returns the app it drove, or None when neither is running."""
    verb = {"next": "next track", "previous": "previous track", "back": "previous track"}.get(
        command, "playpause")
    for app in ("Spotify", "Music"):
        if app_running(app) and osascript(f"tell application {_q(app)} to {verb}")[0]:
            return app
    return None


# ---- system state ----

def cpu_ticks():
    """(idle, total) processor ticks since boot; diff two readings for a percentage."""
    ticks = (ctypes.c_uint32 * 4)()
    count = ctypes.c_uint32(4)
    if _LIBC.host_statistics(_HOST, 3, ticks, ctypes.byref(count)) != 0:   # HOST_CPU_LOAD_INFO
        return 0, 0
    user, system, idle, nice = ticks
    return idle, user + system + idle + nice


class _VMStats64(ctypes.Structure):
    _fields_ = [
        ("free_count", ctypes.c_uint32), ("active_count", ctypes.c_uint32),
        ("inactive_count", ctypes.c_uint32), ("wire_count", ctypes.c_uint32),
        ("zero_fill_count", ctypes.c_uint64), ("reactivations", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64), ("pageouts", ctypes.c_uint64),
        ("faults", ctypes.c_uint64), ("cow_faults", ctypes.c_uint64),
        ("lookups", ctypes.c_uint64), ("hits", ctypes.c_uint64),
        ("purges", ctypes.c_uint64), ("purgeable_count", ctypes.c_uint32),
        ("speculative_count", ctypes.c_uint32), ("decompressions", ctypes.c_uint64),
        ("compressions", ctypes.c_uint64), ("swapins", ctypes.c_uint64),
        ("swapouts", ctypes.c_uint64), ("compressor_page_count", ctypes.c_uint32),
        ("throttled_count", ctypes.c_uint32), ("external_page_count", ctypes.c_uint32),
        ("internal_page_count", ctypes.c_uint32),
        ("total_uncompressed_pages_in_compressor", ctypes.c_uint64),
    ]


def _sysctl_u64(name):
    value = ctypes.c_uint64(0)
    size = ctypes.c_size_t(8)
    _LIBC.sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0)
    return value.value


_MEM_TOTAL = _sysctl_u64("hw.memsize")
_PAGE_SIZE = _sysctl_u64("hw.pagesize") or 4096


def ram_percent():
    """Memory used the way Activity Monitor counts it: app memory, wired and compressed."""
    vm = _VMStats64()
    count = ctypes.c_uint32(ctypes.sizeof(_VMStats64) // 4)
    if _LIBC.host_statistics64(_HOST, 4, ctypes.byref(vm), ctypes.byref(count)) != 0 or not _MEM_TOTAL:
        return 0
    used_pages = (vm.internal_page_count - vm.purgeable_count + vm.wire_count
                  + vm.compressor_page_count)
    return max(0, min(100, used_pages * _PAGE_SIZE * 100 / _MEM_TOTAL))


def disk_percent():
    # "/" is the sealed, read-only system volume; your files live on the Data volume.
    root = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
    usage = shutil.disk_usage(root)
    return (usage.total - usage.free) * 100 / usage.total if usage.total else 0


_BATTERY = {"at": 0.0, "value": (None, False)}


def battery_status(max_age=30):
    """(percent or None, plugged_in). Cached — the HUD asks twice a second and
       pmset is a whole process, while the number moves once a minute at most."""
    if time.time() - _BATTERY["at"] < max_age:
        return _BATTERY["value"]
    ok, out = _run(["pmset", "-g", "batt"], timeout=5)
    m = re.search(r"(\d+)%", out)
    value = (int(m.group(1)) if m else None, "AC Power" in out)
    if ok:
        _BATTERY.update(at=time.time(), value=value)
    return value


def volumes():
    """(name, path, free bytes, total bytes) for every mounted disk."""
    out = []
    for name in sorted(os.listdir("/Volumes")):
        path = os.path.join("/Volumes", name)
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        out.append((name, path, usage.free, usage.total))
    return out


# ---- power ----

_POWER = {"timer": None}


def schedule_power(action, seconds):
    """action is "shut down" or "restart". Waits in this process so it can be
       called off — which also means quitting David calls it off."""
    cancel_power()
    timer = threading.Timer(float(seconds),
                            lambda: osascript(f'tell application "System Events" to {action}'))
    timer.daemon = True
    timer.start()
    _POWER["timer"] = timer


def cancel_power():
    timer, _POWER["timer"] = _POWER["timer"], None
    if timer is None:
        return False
    timer.cancel()
    return True


def sleep_now():
    return _run(["pmset", "sleepnow"])[0]


def lock_screen():
    if can_control():
        press("q", "ctrl", "cmd")
        return True
    return _run(["pmset", "displaysleepnow"])[0]


# ---- files ----

def move_to_trash(path):
    """Into the Trash, where Finder's Put Back still works. Falls back to
       moving it there by hand if Finder can't be asked."""
    if osascript(f"tell application \"Finder\" to delete (POSIX file {_q(path)} as alias)")[0]:
        return os.path.basename(path)
    dest = os.path.join(os.path.expanduser("~/.Trash"), os.path.basename(path))
    if os.path.exists(dest):
        base, ext = os.path.splitext(dest)
        dest = f"{base} {time.strftime('%H.%M.%S')}{ext}"
    shutil.move(path, dest)
    return os.path.basename(dest)


def reminder_script(title, when, notes=""):
    """AppleScript for a Reminders entry due, and alerting, at `when`.
       The day is set to 1 before the month changes, so moving from a 31st
       into a 30-day month can't roll the date over."""
    return (
        "set d to current date\n"
        "set day of d to 1\n"
        f"set year of d to {when.year}\n"
        f"set month of d to {when.month}\n"
        f"set day of d to {when.day}\n"
        f"set time of d to {when.hour * 3600 + when.minute * 60}\n"
        'tell application "Reminders"\n'
        f"  tell default list to make new reminder with properties "
        f"{{name:{_q(title)}, body:{_q(notes)}, due date:d, remind me date:d}}\n"
        "end tell")


def add_reminder(title, when, notes=""):
    """False when Reminders can't be reached — usually macOS Automation
       permission, which it asks for the first time (hence the long timeout)."""
    return osascript(reminder_script(title, when, notes), timeout=30)[0]


def empty_trash():
    return osascript('tell application "Finder" to empty the trash', timeout=60)[0]
