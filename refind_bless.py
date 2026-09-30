#!/usr/bin/env python3
"""refind-bless — a tiny Tkinter app that tells rEFInd which OS to boot next.

Reads the rEFInd menu from the EFI System Partition and writes the
selection to rEFInd's PreviousBoot EFI variable, which rEFInd preselects
on the next boot when `default_selection` is '+' (its out-of-the-box
behaviour).  The only edit ever made to refind.conf is its
`default_selection` line (a .bak copy is kept).

Works on Linux (pkexec for the privileged bits) and Windows (run the
.bat, which self-elevates).  See README.md.
"""

import argparse
import base64
import json
import os
import re
import struct
import subprocess
import sys

__version__ = "0.1"

APP_TITLE = "refind-bless"
REFIND_GUID = "36d08fa7-cf0b-42f5-8f14-68df73ed3740"
EFIVAR_PATH = "/sys/firmware/efi/efivars/PreviousBoot-" + REFIND_GUID
IS_WINDOWS = os.name == "nt"

ACCENT = "#3584e4"
EMOJI = {"linux": "\U0001F427", "windows": "\U0001FA9F",
         "macos": "\U0001F34E"}  # 🐧 🪟 🍎

# --------------------------------------------------------------------------
# refind.conf parsing (shared, pure functions)
# --------------------------------------------------------------------------

def split_tokens(line):
    """Split a refind.conf line into tokens; double quotes group words,
    an unquoted # starts a comment."""
    toks, cur, inq = [], "", False
    for ch in line.strip():
        if ch == '"':
            inq = not inq
        elif ch == "#" and not inq and not cur:
            break
        elif ch.isspace() and not inq:
            if cur:
                toks.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        toks.append(cur)
    return toks


def parse_config(path, depth=0):
    """Return (options, menu entries) from refind.conf, following includes."""
    opts, entries = {}, []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return opts, entries
    i = 0
    while i < len(lines):
        t = split_tokens(lines[i])
        if not t:
            i += 1
            continue
        key = t[0].lower()
        if key == "menuentry" and len(t) > 1:
            entry = {"title": t[1], "loader": "", "icon": "", "volume": "",
                     "ostype": "", "disabled": False}
            # Walk the stanza as a token stream (one "statement" per line,
            # except that `{` / `}` may share a line with other tokens) so
            # both multi-line and one-liner stanzas parse.
            depth, sub = 0, 0
            toks = t[2:]
            opened = False
            while True:
                if not toks:
                    i += 1
                    if i >= len(lines):
                        break
                    toks = split_tokens(lines[i])
                    continue
                if toks[0] == "{":
                    depth += 1
                    opened = True
                    toks = toks[1:]
                    continue
                if toks[0] == "}":
                    depth -= 1
                    toks = toks[1:]
                    if depth <= 0:
                        break
                    sub -= 1
                    continue
                if not opened:  # stray tokens before `{`
                    toks = toks[1:]
                    continue
                kk = toks[0].lower()
                if kk == "submenuentry":
                    sub += 1
                    toks = toks[2:] if len(toks) > 1 else []
                    continue
                if sub == 0 and kk == "disabled":
                    entry["disabled"] = True
                    toks = toks[1:]
                elif sub == 0 and kk in ("loader", "icon", "volume", "ostype") \
                        and len(toks) > 1:
                    entry[kk] = toks[1]
                    toks = toks[2:]
                elif "{" in toks or "}" in toks:
                    toks = toks[1:]  # unknown key; keep scanning for braces
                else:
                    toks = []  # unknown statement: rest of line is its args
            entries.append(entry)
        elif key == "include" and len(t) > 1 and depth < 4:
            inc = os.path.join(os.path.dirname(path), t[1].replace("\\", "/"))
            o, e = parse_config(inc, depth + 1)
            for k, v in o.items():
                opts.setdefault(k, v)
            entries += e
        else:
            opts[key] = t[1:]
        i += 1
    return opts, entries


def classify(title, loader, icon, ostype=""):
    """Best-effort OS family for an entry: linux / windows / macos."""
    if ostype:
        o = ostype.lower()
        if o in ("windows", "xom"):
            return "windows"
        if o == "macos":
            return "macos"
        return "linux"
    t = title.lower()
    l = loader.lower().replace("\\", "/")
    ic = icon.lower()
    if "bootmgfw" in l or "windows" in t or "os_win" in ic:
        return "windows"
    if "coreservices" in l or "os_mac" in ic or re.search(r"mac\s?os|osx", t):
        return "macos"
    return "linux"


ICON_GUESS = {
    "windows": ("os_win8.png", "os_win.png"),  # rEFInd's order for bootmgfw
    "linux": ("os_linux.png", "os_ubuntu.png", "os_arch.png", "os_fedora.png",
              "os_debian.png"),
    "macos": ("os_mac.png", "os_clover.png"),
}

# Loader directories on the ESP that are other boot managers, not Linux.
# Clover/OpenCore are macOS-side loaders, so they get the apple family.
MAC_LOADERS = {"clover": "Clover", "oc": "OpenCore", "opencore": "OpenCore"}


def load_icon_b64(esp, entry):
    """The entry's own icon, else a sensible one from rEFInd's icon set."""
    cands = []
    if entry.get("icon"):
        cands.append(os.path.join(esp, entry["icon"].replace("\\", "/").lstrip("/")))
    icons = os.path.join(esp, "EFI", "refind", "icons")
    # rEFInd's own rule for auto-detected loaders: os_<directory>.png,
    # then os_<loader stem>.png, before falling back to the OS family.
    for hint in entry.get("icon_hints", ()):
        cands.append(os.path.join(icons, "os_%s.png" % hint))
    for name in ICON_GUESS.get(entry["ostype"], ()):
        cands.append(os.path.join(icons, name))
    for c in cands:
        try:
            with open(c, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except OSError:
            pass
    return None


SKIP_DIRS = {"refind", "boot", "tools", "keys", "drivers", "shell",
             "memtest", "memtest86"}


def auto_scan(esp):
    """Fallback when refind.conf has no manual stanzas (rEFInd auto-detects):
    guess the OS list from the ESP's loader directories."""
    entries = []
    efidir = os.path.join(esp, "EFI")
    try:
        names = sorted(os.listdir(efidir))
    except OSError:
        return entries
    for name in names:
        d = os.path.join(efidir, name)
        if name.lower() in SKIP_DIRS or not os.path.isdir(d):
            continue
        if name.lower() == "microsoft":
            if os.path.isfile(os.path.join(d, "Boot", "bootmgfw.efi")):
                # rEFInd titles this entry "Boot Microsoft EFI boot from <vol>"
                # and default_selection/+ matches a substring of the *title*,
                # so the loader filename would never match.
                entries.append({"title": "Windows", "match": "Microsoft EFI boot",
                                "ostype": "windows", "icon": "", "disabled": False})
            continue
        loader = None
        for root, _dirs, files in os.walk(d):
            for f in sorted(files):
                if f.lower().endswith(".efi"):
                    loader = f
                    break
            if loader:
                break
        if loader:
            low = name.lower()
            stem = os.path.splitext(loader)[0].lower()
            entries.append({"title": MAC_LOADERS.get(low, name.capitalize()),
                            "match": loader,
                            "ostype": "macos" if low in MAC_LOADERS else "linux",
                            "icon": "", "icon_hints": [low, stem],
                            "disabled": False})
    # Linux booted straight off its own volume (rEFInd kernel auto-detect):
    # no loader on the ESP.  rEFInd titles that entry "Boot vmlinuz-... from
    # <vol>", so "vmlinuz" is the substring to bless.
    if not any(e["ostype"] == "linux" for e in entries):
        pretty = None
        if not IS_WINDOWS:
            import glob
            if glob.glob("/boot/vmlinuz*"):
                pretty = "Linux"
                try:
                    with open("/etc/os-release") as f:
                        for ln in f:
                            if ln.startswith("PRETTY_NAME="):
                                pretty = ln.split("=", 1)[1].strip().strip('"')
                except OSError:
                    pass
        if pretty is None and has_linux_fs_driver(esp):
            # We can't read the Linux partition from here, but rEFInd can
            # (it has an ext4/btrfs/... driver), so it will offer the kernel.
            pretty = "Linux"
        if pretty:
            entries.insert(0, {"title": pretty, "match": "vmlinuz",
                               "ostype": "linux", "icon": "", "disabled": False})
    return entries


def has_linux_fs_driver(esp):
    """True if rEFInd has a Linux filesystem driver installed, i.e. it scans
    Linux partitions for kernels (rEFInd's kernel auto-detect)."""
    for sub in ("drivers_x64", "drivers_aa64", "drivers_ia32", "drivers"):
        try:
            names = os.listdir(os.path.join(esp, "EFI", "refind", sub))
        except OSError:
            continue
        for n in names:
            if re.match(r"(ext[234]|btrfs|xfs|reiserfs)", n.lower()):
                return True
    return False


def read_previous_boot(esp, use_nvram):
    """Current PreviousBoot value (NVRAM first, then rEFInd's vars file)."""
    def from_nvram():
        if IS_WINDOWS:
            return win_get_firmware_var("PreviousBoot")
        try:
            with open(EFIVAR_PATH, "rb") as f:
                return f.read()[4:]  # skip the 4 attribute bytes
        except OSError:
            return None

    def from_varfile():
        try:
            with open(os.path.join(esp, "EFI", "refind", "vars",
                                   "PreviousBoot"), "rb") as f:
                return f.read()
        except OSError:
            return None

    order = (from_nvram, from_varfile) if use_nvram else (from_varfile,
                                                          from_nvram)
    data = order[0]() or order[1]()
    if data is None:
        return None
    try:
        return data.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        return data.decode("latin-1", "ignore").rstrip("\x00")


def take_snapshot(esp, testing=False):
    """Everything the GUI needs, JSON-safe (icons as base64).
    testing=True (the --esp override) reads PreviousBoot from the tree,
    not from firmware NVRAM."""
    conf = os.path.join(esp, "EFI", "refind", "refind.conf")
    if not os.path.isfile(conf):
        raise RuntimeError("no rEFInd installation found under %s" % esp)
    opts, entries = parse_config(conf)
    for e in entries:
        e["ostype"] = classify(e["title"], e["loader"], e["icon"], e["ostype"])
        e["match"] = e["title"]  # PreviousBoot stores the menu entry title
    auto = not any(not e["disabled"] for e in entries)
    if auto:
        entries = auto_scan(esp)
    entries = [e for e in entries
               if not e["disabled"]
               and e["ostype"] in ("linux", "windows", "macos")]
    for e in entries:
        e["icon_b64"] = load_icon_b64(esp, e)
    ds = opts.get("default_selection") or [None]
    use_nvram = (opts.get("use_nvram", ["true"])[0].lower()
                 not in ("false", "off", "0"))
    return {
        "ok": True,
        "esp": esp,
        "auto_scanned": auto,
        "use_nvram": use_nvram,
        "default_selection": ds[0],
        "previous_boot": read_previous_boot(esp, use_nvram and not testing),
        "entries": entries,
    }


# --------------------------------------------------------------------------
# Linux backend
# --------------------------------------------------------------------------

def find_esp_linux():
    for c in ("/boot/efi", "/efi", "/boot"):
        try:
            if os.path.isfile(os.path.join(c, "EFI", "refind", "refind.conf")):
                return c
        except OSError:
            pass
    for c in ("/boot/efi", "/efi"):
        if os.path.ismount(c):
            return c  # probably just unreadable without root
    return None


def self_pkexec(*args):
    cmd = ["pkexec", sys.executable, os.path.abspath(__file__)] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode in (126, 127):
        raise RuntimeError("authorization was cancelled")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip()
                           or "helper failed (exit %d)" % r.returncode)
    return json.loads(r.stdout)


def linux_snapshot():
    esp = find_esp_linux()
    if esp is None:
        raise RuntimeError("EFI System Partition with rEFInd not found.\n"
                           "Mount it at /boot/efi (or /efi) and retry.")
    if os.access(os.path.join(esp, "EFI", "refind"), os.R_OK):
        return take_snapshot(esp)
    return self_pkexec("--helper", "snapshot")  # ESP readable by root only


def linux_bless(match, default=None):
    if os.geteuid() == 0:
        helper_bless(match, esp=None, default=default)
    else:
        extra = ["--set-default", default] if default else []
        self_pkexec("--helper", "bless", match, *extra)


def helper_bless(match, esp=None, default=None):
    """Runs as root: write PreviousBoot.  The ESP itself is written only in
    the (rare) use_nvram-false configuration.  With an explicit `esp`
    (the --esp test override) NVRAM is never touched: the variable goes
    into that tree's EFI/refind/vars/ instead."""
    testing = esp is not None
    esp = esp or find_esp_linux()
    if esp is None:
        raise RuntimeError("ESP not found")
    if default:
        helper_setdefault(default, esp)
    conf = os.path.join(esp, "EFI", "refind", "refind.conf")
    opts, _ = parse_config(conf)
    use_nvram = (opts.get("use_nvram", ["true"])[0].lower()
                 not in ("false", "off", "0"))
    payload = match.encode("utf-16-le") + b"\x00\x00"
    if use_nvram and not testing and os.path.isdir(os.path.dirname(EFIVAR_PATH)):
        if os.path.exists(EFIVAR_PATH):
            subprocess.run(["chattr", "-i", EFIVAR_PATH], check=False,
                           capture_output=True)
        with open(EFIVAR_PATH, "wb") as f:
            # efivarfs wants attributes + data in a single write()
            f.write(struct.pack("<I", 0x7) + payload)
    else:
        vardir = os.path.join(esp, "EFI", "refind", "vars")
        os.makedirs(vardir, exist_ok=True)
        with open(os.path.join(vardir, "PreviousBoot"), "wb") as f:
            f.write(payload)


def conf_files(esp):
    """refind.conf plus its direct includes."""
    refdir = os.path.join(esp, "EFI", "refind")
    conf = os.path.join(refdir, "refind.conf")
    files = [conf]
    try:
        with open(conf, encoding="utf-8", errors="replace") as f:
            for t in (split_tokens(l) for l in f):
                if t and t[0].lower() == "include" and len(t) > 1:
                    files.append(os.path.join(refdir, t[1].replace("\\", "/")))
    except OSError:
        pass
    return files


def helper_setdefault(value, esp=None):
    """The ONE kind of config edit the app makes: rewrite (or add) the
    `default_selection` line.  value is '+' or an entry title substring.
    A refind.conf.bak backup is kept."""
    esp = esp or find_esp_linux()
    if esp is None:
        raise RuntimeError("ESP not found")
    # refind.conf has no escape for '"'; it's a substring match, so drop it
    value = value.replace('"', "")
    quoted = value if value == "+" else '"%s"' % value
    new_line = "default_selection " + quoted
    for path in conf_files(esp):
        try:
            # surrogateescape: non-UTF-8 bytes are written back unchanged
            with open(path, encoding="utf-8", errors="surrogateescape",
                      newline="") as f:
                raw = f.read()
        except OSError:
            continue
        nl = "\r\n" if "\r\n" in raw else "\n"
        lines = raw.split(nl)
        changed = False
        for i, line in enumerate(lines):
            t = split_tokens(line)
            if t and t[0].lower() == "default_selection":
                if t[1:2] == [value]:
                    return path  # already set
                was = line.split("# was:", 1)
                orig = was[1].strip() if len(was) > 1 else line.strip()
                lines[i] = new_line + "   # was: " + orig
                changed = True
        if not changed and path.endswith("refind.conf"):
            if lines and lines[-1] == "":
                lines.insert(-1, new_line)
            else:
                lines.append(new_line)
            changed = True
        if changed:
            with open(path + ".bak", "w", encoding="utf-8",
                      errors="surrogateescape", newline="") as f:
                f.write(raw)
            with open(path, "w", encoding="utf-8",
                      errors="surrogateescape", newline="") as f:
                f.write(nl.join(lines))
            return path
    raise RuntimeError("refind.conf not found")


# --------------------------------------------------------------------------
# Windows backend (ctypes; the .bat launcher runs us elevated)
# --------------------------------------------------------------------------

def win_is_admin():
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def win_enable_privilege():
    import ctypes
    from ctypes import wintypes

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD),
                    ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    advapi, kernel = ctypes.windll.advapi32, ctypes.windll.kernel32
    token = wintypes.HANDLE()
    advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x28,  # ADJUST|QUERY
                            ctypes.byref(token))
    luid = LUID()
    advapi.LookupPrivilegeValueW(None, "SeSystemEnvironmentPrivilege",
                                 ctypes.byref(luid))
    tp = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(
        LUID_AND_ATTRIBUTES(luid, 0x2)))  # SE_PRIVILEGE_ENABLED
    advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
    kernel.CloseHandle(token)


def win_get_firmware_var(name):
    import ctypes
    win_enable_privilege()
    buf = ctypes.create_string_buffer(4096)
    n = ctypes.windll.kernel32.GetFirmwareEnvironmentVariableW(
        name, "{%s}" % REFIND_GUID, buf, 4096)
    return buf.raw[:n] if n else None


def win_set_firmware_var(name, data):
    import ctypes
    win_enable_privilege()
    ok = ctypes.windll.kernel32.SetFirmwareEnvironmentVariableW(
        name, "{%s}" % REFIND_GUID, data, len(data))
    if not ok:
        raise ctypes.WinError()


def win_mount_esp():
    """Mount the ESP on a free drive letter; return (root, letter)."""
    letter = next((L + ":" for L in "ZYXWVUTSRQP"
                   if not os.path.exists(L + ":\\")), None)
    if letter is None:
        raise RuntimeError("no free drive letter")
    r = subprocess.run(["mountvol", letter, "/S"], capture_output=True,
                       text=True)
    if r.returncode != 0:
        raise RuntimeError("mountvol failed: " +
                           (r.stderr.strip() or r.stdout.strip()))
    return letter + "\\", letter


def windows_snapshot():
    root, letter = win_mount_esp()
    try:
        return take_snapshot(root)
    finally:
        subprocess.run(["mountvol", letter, "/D"], capture_output=True)


def windows_bless(match, use_nvram, default=None):
    payload = match.encode("utf-16-le") + b"\x00\x00"
    if use_nvram:
        win_set_firmware_var("PreviousBoot", payload)
    if default or not use_nvram:
        root, letter = win_mount_esp()
        try:
            if default:
                helper_setdefault(default, root)
            if not use_nvram:
                vardir = os.path.join(root, "EFI", "refind", "vars")
                os.makedirs(vardir, exist_ok=True)
                with open(os.path.join(vardir, "PreviousBoot"), "wb") as f:
                    f.write(payload)
        finally:
            subprocess.run(["mountvol", letter, "/D"], capture_output=True)


# --------------------------------------------------------------------------
# Shared frontend plumbing
# --------------------------------------------------------------------------

def cache_path():
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    else:
        base = os.environ.get("XDG_CACHE_HOME",
                              os.path.expanduser("~/.cache"))
    return os.path.join(base, "refind-bless.json")


def get_snapshot(demo=False):
    """Returns (snapshot, from_cache)."""
    if demo:
        return {"ok": True, "esp": "(demo)", "auto_scanned": False,
                "use_nvram": True, "default_selection": "+",
                "previous_boot": "Linux Demo",
                "entries": [
                    {"title": "Linux Demo", "match": "Linux Demo",
                     "ostype": "linux", "icon_b64": None, "disabled": False},
                    {"title": "Windows Demo", "match": "Windows Demo",
                     "ostype": "windows", "icon_b64": None, "disabled": False},
                ]}, False
    # checked before the cache fallback: a cached menu would load fine and
    # then fail obscurely at Reboot time
    if IS_WINDOWS and not win_is_admin():
        raise RuntimeError("Administrator rights required.\n"
                           "Start the app with refind-bless.bat.")
    try:
        snap = windows_snapshot() if IS_WINDOWS else linux_snapshot()
        try:
            os.makedirs(os.path.dirname(cache_path()), exist_ok=True)
            with open(cache_path(), "w") as f:
                json.dump(snap, f)
        except OSError:
            pass
        return snap, False
    except Exception as e:
        try:
            with open(cache_path()) as f:
                return json.load(f), True
        except OSError:
            raise e


def bless(snap, match, demo=False, default=None):
    if demo:
        return
    # If this entry is the one rEFInd last booted, reuse rEFInd's own exact
    # title rather than our substring guess.
    prev = snap.get("previous_boot") or ""
    if match.lower() in prev.lower():
        match = prev.strip()
    # default: None = leave refind.conf alone; "+" or a title substring
    # = rewrite its default_selection line.
    if IS_WINDOWS:
        windows_bless(match, snap["use_nvram"], default)
    else:
        linux_bless(match, default)


def reboot(demo=False):
    if demo:
        return
    if IS_WINDOWS:
        subprocess.Popen(["shutdown", "/r", "/t", "0"])
    else:
        subprocess.Popen(["systemctl", "reboot"])


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def run_gui(demo=False):
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError:
        sys.stderr.write("refind-bless needs Tkinter: install python3-tk "
                         "(Debian/Ubuntu), python3-tkinter (Fedora) or "
                         "tk (Arch).\n")
        return 1

    try:
        snap, cached = get_snapshot(demo)
    except Exception as e:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror(APP_TITLE, "Could not read the rEFInd menu:\n\n%s" % e)
        return 1
    entries = snap["entries"]
    if not entries:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror(APP_TITLE, "No bootable entries found.")
        return 1

    root = tk.Tk()
    root.title(APP_TITLE)
    root.resizable(False, False)
    bg = root.cget("bg")

    grid = tk.Frame(root, padx=16, pady=16)
    grid.pack()

    ds = snap.get("default_selection")
    state = {"sel": 0}
    pinned = tk.BooleanVar(value=ds not in (None, "+"))
    buttons, photos = [], []

    def select(i, user=True):
        # choosing a different OS is a one-off by default: drop the
        # "always" pin so the user has to opt in again for the new entry
        if user and i != state["sel"]:
            pinned.set(False)
        state["sel"] = i
        for j, b in enumerate(buttons):
            b.config(bg=ACCENT if j == i else bg,
                     activebackground=ACCENT if j == i else bg)

    def go(_event=None):
        e = entries[state["sel"]]
        # unchecked: rEFInd must be in "+" (last-booted) mode for the bless
        # to count; checked: pin this entry as the permanent default.
        if pinned.get():
            want = e["match"]
        else:
            want = "+"
        default = want if ds != want else None
        try:
            bless(snap, e["match"], demo, default)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, "Could not set next boot:\n\n%s" % exc)
            return
        if demo:
            messagebox.showinfo(APP_TITLE,
                                "(demo) would set PreviousBoot=%r%s and reboot"
                                % (e["match"], ", default_selection=%r"
                                   % default if default else ""))
            return
        reboot()
        root.destroy()

    for i, e in enumerate(entries):
        kwargs = {}
        if e.get("icon_b64"):
            img = tk.PhotoImage(data=e["icon_b64"])
            f = max(1, round(img.width() / 96))
            if f > 1:
                img = img.subsample(f, f)
            photos.append(img)
            kwargs = {"image": img, "compound": "top"}
        else:
            kwargs = {"font": ("", 14)}
            e = dict(e, title=EMOJI.get(e["ostype"], "\U0001F4BF")
                     + "\n" + e["title"])
        b = tk.Button(grid, text=e["title"], relief="flat", bd=0,
                      highlightthickness=0, padx=18, pady=12,
                      command=lambda i=i: select(i), **kwargs)
        b.grid(row=0, column=i, padx=6)
        b.bind("<Double-Button-1>", go)
        buttons.append(b)

    bottom = tk.Frame(root, padx=16, pady=12)
    bottom.pack(fill="x")
    ttk.Button(bottom, text="Reboot", command=go).pack(side="left", padx=(0, 12))
    ttk.Checkbutton(bottom, text="Always boot this by default",
                    variable=pinned).pack(side="left")

    notes = []
    if cached:
        notes.append("showing cached menu (couldn't read the ESP)")
    if snap.get("auto_scanned"):
        notes.append("menu guessed from ESP (no manual stanzas)")
    if ds not in (None, "+"):
        notes.append("current default: %s" % ds)
    prev = snap.get("previous_boot")
    status = "last booted: %s" % (prev or "unknown")
    status = "\n".join([status] + notes)
    tk.Label(root, text=status, fg="#777", font=("", 8), justify="left",
             wraplength=360, padx=16, pady=4).pack(anchor="w")

    # preselect what rEFInd itself will highlight: the pinned
    # default_selection if there is one, else the last-booted entry.
    def find(text):
        if not text:
            return None
        t = text.strip().lower()
        if t.isdigit():  # rEFInd: a bare number is a 1-based position
            return int(t) - 1 if 0 < int(t) <= len(entries) else None
        for i, e in enumerate(entries):
            if e["match"].lower() in t or t in e["title"].lower():
                return i
        return None

    sel = find(ds) if ds not in (None, "+") else None
    if sel is None:
        sel = find(prev)
    select(sel if sel is not None else 0, user=False)

    root.bind("<Return>", go)
    root.bind("<Escape>", lambda e: root.destroy())
    root.bind("<Left>", lambda e: select(max(0, state["sel"] - 1)))
    root.bind("<Right>", lambda e: select(min(len(entries) - 1,
                                              state["sel"] + 1)))

    def to_front():
        root.update_idletasks()
        root.lift()
        root.attributes("-topmost", True)
        root.after_idle(root.attributes, "-topmost", False)
        if IS_WINDOWS:
            # launched via Task Scheduler (no-UAC shortcut) Windows won't
            # let us take the foreground; a synthetic Alt tap lifts that
            import ctypes
            u = ctypes.windll.user32
            u.keybd_event(0x12, 0, 0, 0)
            u.keybd_event(0x12, 0, 2, 0)
            u.SetForegroundWindow(int(root.wm_frame(), 16))
        root.focus_force()
    root.after(50, to_front)
    root.mainloop()
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--helper", choices=["snapshot", "bless"],
                    help="privileged helper mode (used internally via pkexec)")
    ap.add_argument("match", nargs="?", help="entry to bless (helper mode)")
    ap.add_argument("--set-default", metavar="VALUE",
                    help="helper bless: also rewrite default_selection")
    ap.add_argument("--esp", help="ESP root override (for testing)")
    ap.add_argument("--demo", action="store_true",
                    help="fake entries, no privileges needed, no reboot")
    ap.add_argument("--version", action="version",
                    version="refind-bless " + __version__)
    args = ap.parse_args()

    if args.helper == "snapshot":
        esp = args.esp or find_esp_linux()
        if esp is None:
            print("ESP not found", file=sys.stderr)
            return 1
        print(json.dumps(take_snapshot(esp, testing=args.esp is not None)))
        return 0
    if args.helper == "bless":
        if not args.match or not (0 < len(args.match) <= 120) \
                or not args.match.isprintable():
            print("bad entry name", file=sys.stderr)
            return 1
        d = args.set_default
        if d and (d != "+" and (len(d) > 120 or not d.isprintable())):
            print("bad default", file=sys.stderr)
            return 1
        helper_bless(args.match, esp=args.esp, default=d)
        print(json.dumps({"ok": True}))
        return 0
    return run_gui(demo=args.demo)


if __name__ == "__main__":
    sys.exit(main())
