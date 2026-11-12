#!/usr/bin/env python3
"""Phone link for the Omarchy bar: KDE Connect for battery, notifications,
replies, ringing, sharing and pairing, plus ADB for the phone-side toggles,
scrcpy mirroring, the camera and photo import. KDE Connect finds the phone on
the home Wi-Fi by itself, and anywhere else through Tailscale once `tailnet`
has given it the phone's tailnet address. Files go over Taildrop.

With no arguments (or `status`) prints one JSON object for BarWidget.qml.
Every other subcommand is an action behind a button or a shortcut:

  ring | clipboard | pair | accept | reject | unpair <device-id>
  dismiss <device-id> <notification-id>
  reply <device-id> <reply-id>   the reply's text comes on stdin, one line, so
                                 it never shows in the process list
  toggle <bluetooth|dnd|data|location> <on|off>
  wifi-adb           move a USB-connected phone's adb onto Wi-Fi (Android ≤ 10)
  lock-phone
  mirror [package]   mirror the phone, opening an app first if given
  stop-mirror
  open-app <package> show an app in the mirror window, mirroring if need be
  send [file…]       send files to the phone; asks for them when none given
  send-screenshot    send the newest screenshot
  handoff            send the focused browser tab, or else the clipboard
  camera             open the phone's camera; the photo lands on the clipboard
  photos [--quiet]   copy new camera photos to ~/Pictures/Phone
  sms                open KDE Connect's SMS app
  setup-phone        grant KDE Connect on the phone the clipboard, call and
                     SMS permissions (needs ADB)
  ping <message>     show a message on the phone
  vnc-toggle         switch VNC (the omarchy-vnc plugin) and tell the phone how it went
  rescan
  setup-commands     give every paired phone the "Run command" buttons that
                     control this PC (replaces any existing list)
  setup-kdeconnect   keep the pictures in the phone's notifications out of the
                     shared /tmp: KDE Connect gets your runtime folder as its
                     temporary folder, and umask 077 (restarts it)
  tailnet [on|off]   add (or remove) the Tailscale address of every Android
                     device on the tailnet to KDE Connect's custom devices
  lost               the phone is gone: lock it if it's still in reach, then
                     unpair it, forget its tailnet address, drop ADB, and open
                     the Tailscale admin page to remove it from the tailnet

KDE Connect is reached over D-Bus (org.kde.kdeconnect), which starts the
daemon on first call. Android doesn't let ordinary apps flip Bluetooth, data
or location, so those go through `adb shell`: on Android 11+ once Wireless
debugging is paired with `adb pair`, on older phones after plugging in over
USB once per boot and running `wifi-adb`.

Home or away: the optional daemon in daemon/ (or anything else) can
write {"where": "home" | "away" | "unknown", ...} to
$XDG_RUNTIME_DIR/omarchy-phone-presence.json, and the panel shows it.
Without one, the phone counts as home while KDE Connect reaches it over the LAN.
"""

import glob
import html
import ipaddress
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

SERVICE = "org.kde.kdeconnect"
ROOT = "/modules/kdeconnect"
IFACE = "org.kde.kdeconnect"
SCRIPT = os.path.realpath(__file__)
ADB_TIMEOUT = 5

# Tailscale hands out addresses from these ranges.
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
TAILSCALE_ADMIN = "https://login.tailscale.com/admin/machines"

def private_dir(path):
    """Create `path` for this account alone. makedirs' mode only applies to a folder it creates,
    so an existing one (say 0755, from an older version under umask 022) is tightened too."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def runtime_dir():
    """$XDG_RUNTIME_DIR, which is yours alone. Without it, a private folder in ~/.cache, never
    /tmp, where another account could plant files or symlinks under these names first."""
    d = os.environ.get("XDG_RUNTIME_DIR")
    if d and os.path.isdir(d):
        return d
    d = os.path.join(os.path.expanduser("~/.cache"), "omarchy-phone-run")
    return private_dir(d)


RUNTIME = runtime_dir()
# adb's server normally listens on 127.0.0.1:5037, where any account on this machine could use it
# to drive your phone. This plugin's adb (and scrcpy, which runs adb) use a socket in your runtime
# folder instead. Your own ADB_SERVER_SOCKET, if you set one, wins.
os.environ.setdefault("ADB_SERVER_SOCKET",
                      "localfilesystem:" + os.path.join(RUNTIME, "omarchy-phone-adb.sock"))
NUDGE_STAMP = os.path.join(RUNTIME, "omarchy-phone-nudge")
NUDGE_EVERY = 60
RECONNECT_STAMP = os.path.join(RUNTIME, "omarchy-phone-adb-retry")
RECONNECT_EVERY = 30
PRESENCE_FILE = os.path.join(RUNTIME, "omarchy-phone-presence.json")  # written by an optional presence daemon
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "omarchy-phone")

KDECONNECT_APP = "org.kde.kdeconnect_tp"
CAMERA_DIR = "/sdcard/DCIM/Camera"
PHOTOS_DIR = os.path.expanduser("~/Pictures/Phone")
PHOTO_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".heic": "image/heic", ".heif": "image/heif"}
VNC_HELPER = os.path.expanduser("~/.config/omarchy/plugins/io.github.yassinesawahi.vnc/vnc.py")
BROWSERS = ("chrome", "chromium", "brave", "edge", "vivaldi", "firefox", "zen", "librewolf")
PACKAGE = re.compile(r"[A-Za-z][\w]*(\.[\w]+)+")
SAFE_NAME = re.compile(r"[\w][\w.\- ]*")


def find_adb():
    # Android Studio ships its own adb. Two servers of different versions
    # kill each other on every call, so use the SDK's copy when there is one.
    if os.environ.get("ADB"):
        return os.environ["ADB"]
    for sdk in (os.environ.get("ANDROID_HOME"), os.path.expanduser("~/Android/Sdk")):
        if sdk and os.access(os.path.join(sdk, "platform-tools", "adb"), os.X_OK):
            return os.path.join(sdk, "platform-tools", "adb")
    return shutil.which("adb")


ADB = find_adb()

# Each phone-side toggle: the setting that reports it, and the shell commands
# that flip it. The command after `||` is the fallback for older Android.
TOGGLES = {
    "bluetooth": {
        "read": "settings get global bluetooth_on",
        "on": "svc bluetooth enable || cmd bluetooth_manager enable",
        "off": "svc bluetooth disable || cmd bluetooth_manager disable",
    },
    "dnd": {
        "read": "settings get global zen_mode",
        "on": "cmd notification set_dnd on",
        "off": "cmd notification set_dnd off",
    },
    "data": {
        "read": "settings get global mobile_data",
        "on": "svc data enable",
        "off": "svc data disable",
    },
    "location": {
        "read": "settings get secure location_mode",
        "on": "cmd location set-location-enabled true || settings put secure location_mode 3",
        "off": "cmd location set-location-enabled false || settings put secure location_mode 0",
    },
}

# What KDE Connect on the phone needs for the clipboard to flow phone → PC
# (Android 10 hides the clipboard from background apps; with the system log
# it can tell when it changed), for calls to show up here, and for SMS.
PHONE_GRANTS = [
    ("clipboard", ["pm grant {app} android.permission.READ_LOGS",
                   "appops set {app} SYSTEM_ALERT_WINDOW allow"]),
    ("calls", ["pm grant {app} android.permission.READ_PHONE_STATE",
               "pm grant {app} android.permission.READ_CALL_LOG",
               "pm grant {app} android.permission.READ_CONTACTS"]),
    ("messages", ["pm grant {app} android.permission.READ_SMS",
                  "pm grant {app} android.permission.SEND_SMS",
                  "pm grant {app} android.permission.RECEIVE_SMS"]),
]


def notify(title, body, urgency="normal", image=None, open_path=None):
    if (image or open_path) and shutil.which("omarchy-notification-send"):
        cmd = ["omarchy-notification-send", "--app-name", "Phone", "-u", urgency]
        if image:
            cmd += ["--image", image]
        cmd += [title, body]
        if open_path:
            cmd += ["--exec", "xdg-open", open_path]
        subprocess.run(cmd, check=False)  # photo notifications: a title and a path, nothing private
        return
    # Straight over D-Bus rather than through notify-send: the body can be clipboard text, and a
    # command's arguments are readable by any user in the process list.
    hints = {"urgency": GLib.Variant("y", {"low": 0, "normal": 1, "critical": 2}.get(urgency, 1))}
    try:
        bus().call_sync("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                        "org.freedesktop.Notifications", "Notify",
                        GLib.Variant("(susssasa{sv}i)", ("Phone", 0, "phone", title, body, [], hints, -1)),
                        None, Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error:
        pass


def due(stamp, every):
    """True at most once per `every` seconds for a given stamp file."""
    try:
        if time.time() - os.path.getmtime(stamp) < every:
            return False
    except OSError:
        pass
    with open(stamp, "w"):
        pass
    return True


# --- KDE Connect -------------------------------------------------------------

_bus = None


def bus():
    global _bus
    if _bus is None:
        _bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return _bus


def call(path, iface, method, args=None, timeout=3000):
    kdeconnect_gate()  # may start KDE Connect: only once its picture folder is yours alone
    reply = bus().call_sync(SERVICE, ROOT + path, iface, method, args, None,
                            Gio.DBusCallFlags.NONE, timeout, None)
    return reply.unpack() if reply else ()


def props(path, iface):
    try:
        return call(path, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", (iface,)))[0]
    except GLib.Error:
        return None


# --- KDE Connect's temporary folder --------------------------------------------
# KDE Connect saves the pictures in your phone's notifications in $TMPDIR/kdeconnect_$USER, and
# runs with umask 022. Under the shared /tmp another account can create that folder first: KDE
# Connect uses it anyway (its chmod fails silently) and the pictures land there readable by that
# account. `setup-kdeconnect` gives KDE Connect your runtime folder as $TMPDIR, and umask 077.
# Until then, every call() checks that folder before it could start or read KDE Connect: it claims
# the folder for you while nobody else has, and stops KDE Connect if another account owns it.

KDECONNECT_UNIT = "app-org.kde.kdeconnect.daemon@autostart.service"  # from /etc/xdg/autostart
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
DATA_HOME = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
KDECONNECT_DROPIN = os.path.join(CONFIG_HOME, "systemd/user", KDECONNECT_UNIT + ".d", "omarchy-phone-private.conf")
KDECONNECT_DBUS_FILE = os.path.join(DATA_HOME, "dbus-1/services", SERVICE + ".service")
DROPIN_TEXT = """\
# Written by the omarchy-phone plugin (phone.py setup-kdeconnect). KDE Connect keeps the
# pictures in your phone's notifications in $TMPDIR/kdeconnect_$USER; /tmp is shared with every
# account on this PC, so it gets your private runtime folder instead, and a private umask.
[Service]
Environment=TMPDIR=%t
UMask=0077
"""
DBUS_FILE_TEXT = f"""\
# Written by the omarchy-phone plugin (phone.py setup-kdeconnect). When something calls KDE
# Connect while it isn't running, it starts through {KDECONNECT_UNIT},
# with its private temporary folder, rather than straight from D-Bus.
[D-BUS Service]
Name={SERVICE}
Exec=/usr/bin/kdeconnectd
SystemdService={KDECONNECT_UNIT}
"""
STOPPED_STAMP = os.path.join(RUNTIME, "omarchy-phone-kdeconnect-stopped")
STOPPED_NOTICE_EVERY = 600


def kdeconnect_pid():
    """The running KDE Connect daemon's PID, asked of the bus (which never starts it), or None."""
    try:
        reply = bus().call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "GetConnectionUnixProcessID", GLib.Variant("(s)", (SERVICE,)),
                                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 2000, None)
        return reply.unpack()[0]
    except GLib.Error:
        return None


def is_kdeconnectd(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip() == "kdeconnectd"
    except OSError:
        return False


def process_env(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            pairs = f.read().split(b"\0")
    except OSError:
        return None
    return dict(p.decode(errors="replace").split("=", 1) for p in pairs if b"=" in p)


def private_folder(path):
    """Whether `path` is a real folder (not a symlink) that only this account can enter."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077


def pictures_folder(env):
    return os.path.join(env.get("TMPDIR") or "/tmp", "kdeconnect_" + env.get("USER", ""))


def set_up():
    """Whether Keep private is in place, so KDE Connect starts with your runtime folder as $TMPDIR."""
    return os.path.isfile(KDECONNECT_DROPIN) and os.path.isfile(KDECONNECT_DBUS_FILE)


def kdeconnect_env():
    """KDE Connect's PID and environment while it runs, else (None, the $TMPDIR and $USER it
    would start with)."""
    pid = kdeconnect_pid()
    env = process_env(pid) if pid else None
    if env is not None:
        return pid, env
    tmp = os.environ.get("XDG_RUNTIME_DIR") if set_up() else os.environ.get("TMPDIR")
    return None, {"TMPDIR": tmp or "", "USER": os.environ.get("USER", "")}


def stop_kdeconnect(pid):
    """Stop KDE Connect: its autostart unit, and `pid` as well if it started some other way."""
    subprocess.run(["systemctl", "--user", "stop", KDECONNECT_UNIT], capture_output=True, check=False)
    if pid and is_kdeconnectd(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):
            if not is_kdeconnectd(pid):
                break
            time.sleep(0.1)


class Exposed(GLib.Error):
    """KDE Connect would save the pictures in your phone's notifications where another account can
    see them, so it's stopped and left alone until Keep private."""


def check_pictures():
    """Before KDE Connect is started or read: a hint for the panel ("" when all is well), or
    Exposed. Under a shared temporary folder it claims kdeconnect_$USER for you (0700) while
    nobody else has, and tightens yours; if another account owns it, KDE Connect is stopped."""
    pid, env = kdeconnect_env()
    if private_folder(env.get("TMPDIR") or "/tmp"):
        return ""
    folder = pictures_folder(env)
    try:
        os.mkdir(folder, 0o700)
    except OSError:
        pass
    try:
        st = os.lstat(folder)
    except OSError:
        st = None
    if st and stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
        if st.st_mode & 0o077:
            try:  # a real folder of ours: nobody else can swap it in sticky /tmp
                os.chmod(folder, 0o700)
            except OSError:
                pass
        return "KDE Connect keeps the pictures in your phone's notifications in the shared /tmp, " \
               "in a folder only you can open. Keep private moves them to your own folder."
    if pid:  # it would go on saving them there
        stop_kdeconnect(pid)
        if due(STOPPED_STAMP, STOPPED_NOTICE_EVERY):
            notify("KDE Connect stopped", f"Another account owns {folder}, where KDE Connect saves the "
                   "pictures in your phone's notifications. Keep private in the Phone panel moves them "
                   "to your own folder and starts it again.", "critical")
    return Exposed(f"KDE Connect is stopped: another account owns {folder}, where it would save the "
                   "pictures in your phone's notifications. Keep private moves them to your own "
                   "folder and starts KDE Connect again.")


_gate = None  # what check_pictures() found, once per run


def kdeconnect_gate():
    """Run before anything that could start KDE Connect or read from it: every call() does."""
    global _gate
    if _gate is None:
        _gate = check_pictures()
    if isinstance(_gate, Exposed):
        raise _gate
    return _gate


def clear_pictures(folder):
    """Delete the pictures KDE Connect left in `folder`: only regular files of ours, never
    following a symlink, even in a folder another account owns."""
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        for name in os.listdir(fd):
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
                    os.unlink(name, dir_fd=fd)
            except OSError:
                pass
    finally:
        os.close(fd)


def setup_kdeconnect():
    """Run KDE Connect with your runtime folder as its temporary folder and umask 077, whether
    it starts at login or because something called it, then restart it."""
    loaded = subprocess.run(["systemctl", "--user", "show", "-p", "LoadState", "--value", KDECONNECT_UNIT],
                            capture_output=True, text=True, check=False).stdout.strip()
    if loaded != "loaded":
        notify("KDE Connect", f"Nothing changed: there's no {KDECONNECT_UNIT}, so this session's "
               "autostart doesn't start KDE Connect.", "critical")
        return 1
    old, old_env = kdeconnect_env()  # before the files below change where it would start
    for path, text in ((KDECONNECT_DROPIN, DROPIN_TEXT), (KDECONNECT_DBUS_FILE, DBUS_FILE_TEXT)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".new", "w") as f:
            f.write(text)
        os.replace(path + ".new", path)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    try:  # so D-Bus reads the new service file
        bus().call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ReloadConfig",
                        None, None, Gio.DBusCallFlags.NONE, 5000, None)
    except GLib.Error:
        pass
    stop_kdeconnect(old)
    subprocess.run(["systemctl", "--user", "start", KDECONNECT_UNIT], check=False)
    pid = None
    for _ in range(50):
        pid = kdeconnect_pid()
        if pid:
            break
        time.sleep(0.1)
    env = process_env(pid) if pid else None
    if not env or not private_folder(env.get("TMPDIR") or "/tmp"):
        notify("KDE Connect", "Set up, but KDE Connect isn't running that way yet: log out and back in.", "critical")
        return 1
    if not private_folder(old_env.get("TMPDIR") or "/tmp"):  # even if it was stopped for being there
        clear_pictures(pictures_folder(old_env))
    notify("KDE Connect", "Restarted with its temporary folder in your private runtime folder: "
           "the pictures in your phone's notifications are yours alone.")
    return 0


def device_ids():
    return call("", IFACE + ".daemon", "devices", GLib.Variant("(bb)", (False, False)))[0]


def plain(text):
    # Android hands over message bodies as HTML (<br/>, &amp;…).
    text = re.sub(r"<br\s*/?>", " ", text or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def package_of(key):
    """The app behind a notification. Android keys look like 0|com.whatsapp|1|null|10123."""
    parts = (key or "").split("|")
    return parts[1] if len(parts) > 2 and PACKAGE.fullmatch(parts[1]) else ""


def read_notifications(path):
    try:
        ids = call(path + "/notifications", IFACE + ".device.notifications", "activeNotifications")[0]
    except GLib.Error:
        return []
    notes = []
    for nid in reversed(ids):  # newest first
        n = props(f"{path}/notifications/{nid}", IFACE + ".device.notifications.notification")
        if not n:
            continue
        notes.append({
            "id": nid,
            "app": plain(n.get("appName")),
            "title": plain(n.get("title")),
            "text": plain(n.get("text")),
            "dismissable": bool(n.get("dismissable")),
            "replyId": n.get("replyId") or "",
            "package": package_of(n.get("internalId")),
        })
    return notes


def read_device(dev_id):
    path = f"/devices/{dev_id}"
    p = props(path, IFACE + ".device") or {}
    dev = {
        "id": dev_id,
        "name": p.get("name") or dev_id,
        "type": p.get("type", ""),
        "reachable": bool(p.get("isReachable")),
        "paired": bool(p.get("isPaired")),
        "pairRequested": bool(p.get("isPairRequested")),
        "pairRequestedByPeer": bool(p.get("isPairRequestedByPeer")),
        "verificationKey": p.get("verificationKey", ""),
        "via": "",
        "battery": None,
        "charging": False,
        "network": "",
        "signal": None,
        "notifications": [],
    }
    if not (dev["paired"] and dev["reachable"]):
        return dev

    # Any address outside the tailnet means the phone is on this PC's network.
    addrs = p.get("reachableAddresses") or []
    if addrs:
        dev["via"] = "tailnet" if all(map(on_tailnet, addrs)) else "lan"

    b = props(path + "/battery", IFACE + ".device.battery") or {}
    if b.get("charge", -1) >= 0:
        dev["battery"] = b["charge"]
        dev["charging"] = bool(b.get("isCharging"))

    c = props(path + "/connectivity_report", IFACE + ".device.connectivity_report") or {}
    if c.get("cellularNetworkType") not in (None, "", "Unknown"):
        dev["network"] = c["cellularNetworkType"]
    if c.get("cellularNetworkStrength", -1) >= 0:
        dev["signal"] = c["cellularNetworkStrength"]

    dev["notifications"] = read_notifications(path)
    return dev


def reachable_phones():
    """(id, name) of every paired phone KDE Connect can reach right now."""
    try:
        ids = device_ids()
    except GLib.Error:
        return []
    out = []
    for dev_id in ids:
        p = props(f"/devices/{dev_id}", IFACE + ".device") or {}
        if p.get("isPaired") and p.get("isReachable"):
            out.append((dev_id, p.get("name") or dev_id))
    return out


def device_call(dev_id, sub, iface, *methods, args=None):
    """Call the first of `methods` the daemon knows; names moved between releases."""
    err = None
    for method in methods:
        try:
            call(f"/devices/{dev_id}{sub}", IFACE + iface, method, args)
            return 0
        except GLib.Error as e:
            err = e
    print(err.message if err else "no method", file=sys.stderr)
    return 1


def reply(dev_id, reply_id, message):
    return device_call(dev_id, "/notifications", ".device.notifications", "sendReply",
                       args=GLib.Variant("(ss)", (reply_id, message)))


def ping(message):
    phones = reachable_phones()
    for dev_id, _ in phones:
        device_call(dev_id, "/ping", ".device.ping", "sendPing", args=GLib.Variant("(s)", (message,)))
    return 0 if phones else 1


# --- Tailscale ---------------------------------------------------------------

def on_tailnet(addr):
    try:
        ip = ipaddress.ip_address(addr.strip("[]"))
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:  # the daemon's dual-stack socket reports IPv4 peers this way
        ip = ip.ipv4_mapped
    return any(ip in net for net in TAILNET)


def tailnet_phones():
    """{Tailscale IPv4: {name, dns, online}} for every Android device on the tailnet."""
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=3)
        peers = (json.loads(r.stdout).get("Peer") or {}).values()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    return {ip: {"name": p.get("HostName", ""), "dns": (p.get("DNSName") or "").split(".")[0],
                 "online": bool(p.get("Online"))}
            for p in peers if p.get("OS") == "android"
            for ip in p.get("TailscaleIPs") or [] if ":" not in ip}


def custom_devices():
    return list(call("", "org.freedesktop.DBus.Properties", "Get",
                     GLib.Variant("(ss)", (IFACE + ".daemon", "customDevices")))[0])


def set_custom_devices(addrs):
    call("", "org.freedesktop.DBus.Properties", "Set",
         GLib.Variant("(ssv)", (IFACE + ".daemon", "customDevices", GLib.Variant("as", addrs))))


def tailnet(enable):
    """Give KDE Connect the phone's tailnet address on top of LAN discovery,
    so it keeps reaching the phone on mobile data and other Wi-Fi."""
    keep = [a for a in custom_devices() if not on_tailnet(a)]
    phones = sorted(tailnet_phones()) if enable else []
    if enable and not phones:
        print("No Android device on the tailnet. Is Tailscale signed in on the phone?", file=sys.stderr)
        return 1
    set_custom_devices(keep + phones)
    call("", IFACE + ".daemon", "forceOnNetworkChange")
    print("KDE Connect custom devices:", ", ".join(keep + phones) or "none")
    return 0


def read_tailnet(devices):
    """Whether KDE Connect also looks for the phone over Tailscale and, when
    the phone is out of reach, whether Tailscale itself can see it."""
    try:
        custom = [a for a in custom_devices() if on_tailnet(a)]
    except GLib.Error:
        custom = []
    info = {"enabled": bool(custom), "phoneOnline": None}
    if custom and any(d["paired"] and not d["reachable"] for d in devices):
        phones = tailnet_phones()
        info["phoneOnline"] = any(phones.get(a, {}).get("online") for a in custom)
        if info["phoneOnline"]:
            nudge()
    return info


def nudge():
    """Announce this PC again while the phone is up on the tailnet but KDE
    Connect has lost it. The daemon only announces itself when this PC's own
    network changes, and the phone's Tailscale coming back isn't such a change."""
    if not due(NUDGE_STAMP, NUDGE_EVERY):
        return
    try:
        call("", IFACE + ".daemon", "forceOnNetworkChange")
    except GLib.Error:
        pass


def read_presence():
    """Home or away, as the optional presence daemon last saw it."""
    try:
        with open(PRESENCE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# --- ADB ---------------------------------------------------------------------

def adb(*args, timeout=ADB_TIMEOUT):
    if not ADB:
        return None
    try:
        return subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def adb_devices():
    r = adb("devices")
    if not r:
        return []
    found = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        # Android Studio's emulators are not the phone.
        if len(parts) >= 2 and not parts[0].startswith("emulator-"):
            found.append((parts[0], parts[1]))
    return found


def is_wireless(serial):
    return "._adb-tls-connect." in serial or ":" in serial


def phone_addresses():
    """LAN addresses KDE Connect currently reaches paired phones on."""
    try:
        ids = device_ids()
    except GLib.Error:
        return []
    addrs = []
    for dev_id in ids:
        p = props(f"/devices/{dev_id}", IFACE + ".device") or {}
        if p.get("isPaired") and p.get("isReachable"):
            addrs += [f"[{a}]" if ":" in a else a for a in p.get("reachableAddresses", [])]
    return addrs


def adb_target(eager=True):
    """(serial, state) of the phone, preferring a ready wireless link. Not
    eager: reconnect attempts happen at most every RECONNECT_EVERY seconds."""
    devices = adb_devices()
    if not devices and (eager or due(RECONNECT_STAMP, RECONNECT_EVERY)):
        # Android 11+: a phone paired for Wireless debugging normally
        # auto-connects over mDNS; connect explicitly in case that's off.
        r = adb("mdns", "services")
        for line in r.stdout.splitlines() if r else []:
            parts = line.split()
            if len(parts) >= 3 and parts[1].startswith("_adb-tls-connect."):
                adb("connect", parts[2])
        # Android 10 and older: after `wifi-adb` (adb tcpip) the phone keeps
        # listening on 5555 until it reboots, at the address KDE Connect sees,
        # and at its tailnet address wherever it is.
        if not adb_devices():
            for addr in phone_addresses() + sorted(tailnet_phones()):
                r = adb("connect", f"{addr}:5555")
                if r and "connected to" in r.stdout:
                    break
        devices = adb_devices()
    if not devices:
        return None, None

    def rank(d):
        serial, state = d
        return (state != "device", not is_wireless(serial))

    return sorted(devices, key=rank)[0]


def wifi_adb():
    """Move a USB-connected phone's adb onto Wi-Fi (port 5555) and connect."""
    serial, state = adb_target()
    if state != "device":
        notify("Phone not connected over USB", "Plug it in with USB debugging on and allow this PC.")
        return 1
    if is_wireless(serial):
        return 0
    addrs = phone_addresses()
    if not addrs:
        r = adb("-s", serial, "shell", "ip -f inet addr show wlan0")
        addrs = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", r.stdout if r else "")
    if not addrs:
        notify("Phone isn't on Wi-Fi", "Connect it to the same network as this PC.")
        return 1
    adb("-s", serial, "tcpip", "5555")
    for _ in range(10):
        time.sleep(1)
        r = adb("connect", f"{addrs[0]}:5555")
        if r and "connected to" in r.stdout:
            notify("ADB over Wi-Fi is on", "You can unplug the phone. Repeat after it reboots.", "low")
            return 0
    notify("Couldn't reach the phone over Wi-Fi", f"adb connect {addrs[0]}:5555 failed.")
    return 1


def read_adb():
    serial, state = adb_target(eager=False)
    if not serial:
        return None
    info = {"serial": serial, "state": state, "wireless": is_wireless(serial), "toggles": {}}
    if state != "device":
        return info
    keys = list(TOGGLES)
    r = adb("-s", serial, "shell", "; ".join(TOGGLES[k]["read"] for k in keys))
    values = r.stdout.splitlines() if r and r.returncode == 0 else []
    for key, value in zip(keys, values):
        value = value.strip()
        if value in ("", "null"):
            continue  # this phone doesn't keep that setting (e.g. no SIM)
        info["toggles"][key] = value == "1" if key == "bluetooth" else value != "0"
    return info


def ready_adb(why):
    """Serial of a usable ADB link, or None after telling the user why not."""
    serial, state = adb_target()
    if state == "device":
        return serial
    notify(f"Can't {why}", "ADB isn't connected. Plug the phone in once with USB debugging on.")
    return None


def adb_shell(command):
    serial, state = adb_target()
    if state != "device":
        notify("Phone not reachable over ADB", "Plug it in with USB debugging on, or turn on Wireless debugging (Android 11+).")
        return 1
    r = adb("-s", serial, "shell", command)
    if not r or r.returncode != 0:
        print(r.stderr if r else "adb failed", file=sys.stderr)
        return 1
    return 0


def mirror(package=None):
    serial = ready_adb("mirror the phone")
    if not serial:
        return 1
    if ADB:
        os.environ["ADB"] = ADB  # scrcpy must talk to the same adb server
    names = [name for _, name in reachable_phones()]
    # The phone's own screen stays dark while it shows here, and goes off
    # again when the window closes, so it can stay in a pocket.
    args = ["scrcpy", "-s", serial, "--turn-screen-off", "--power-off-on-close",
            "--window-title", names[0] if names else "Phone"]
    if package:
        args.append(f"--start-app={package}")
    os.execvp("scrcpy", args)


def mirroring():
    return subprocess.run(["pgrep", "-x", "scrcpy"], capture_output=True).returncode == 0


def open_app(package):
    if not PACKAGE.fullmatch(package or ""):
        return 2
    if mirroring():  # already on screen: just bring the app up in it
        return adb_shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
    return mirror(package)


def setup_phone():
    serial = ready_adb("set up the phone")
    if not serial:
        return 1
    granted, failed = [], []
    for label, commands in PHONE_GRANTS:
        results = [adb("-s", serial, "shell", c.format(app=KDECONNECT_APP)) for c in commands]
        ok = all(r and r.returncode == 0 and "Exception" not in r.stderr + r.stdout for r in results)
        (granted if ok else failed).append(label)
    # KDE Connect only notices the log permission after a restart.
    adb("-s", serial, "shell", f"am force-stop {KDECONNECT_APP}; "
        f"monkey -p {KDECONNECT_APP} -c android.intent.category.LAUNCHER 1")
    notify("Phone set up" if not failed else "Phone partly set up",
           ("Granted KDE Connect: " + ", ".join(granted) + ". " if granted else "")
           + ("Couldn't grant: " + ", ".join(failed) + "." if failed else "Copy on the phone, paste here."),
           "normal" if not failed else "critical")
    return 0 if not failed else 1


# --- Camera and photos --------------------------------------------------------

def camera_photos(serial):
    """(mtime, name) of every photo in the phone's camera folder."""
    r = adb("-s", serial, "shell", f"stat -c '%Y %n' {CAMERA_DIR}/*", timeout=30)
    photos = []
    for line in r.stdout.splitlines() if r and r.returncode == 0 else []:
        mtime, _, path = line.partition(" ")
        name = path.rpartition("/")[2]
        if mtime.isdigit() and SAFE_NAME.fullmatch(name) and os.path.splitext(name)[1].lower() in PHOTO_TYPES:
            photos.append((int(mtime), name))
    return photos


def pull_photo(serial, name):
    """Copy one photo to PHOTOS_DIR once the camera has finished writing it."""
    private_dir(PHOTOS_DIR)  # your photos: yours alone
    src = f"{CAMERA_DIR}/{name}"
    last = -1
    for _ in range(12):
        r = adb("-s", serial, "shell", f"stat -c %s '{src}'")
        size = int(r.stdout.strip()) if r and r.stdout.strip().isdigit() else -1
        if size > 0 and size == last:
            break
        last = size
        time.sleep(0.5)
    dest = os.path.join(PHOTOS_DIR, name)
    # adb creates the file, then (with -a) applies the phone's mode, both masked by the umask:
    # 077 keeps the photo 0600 from its first byte, whatever the phone's mode was.
    old = os.umask(0o077)
    try:
        r = adb("-s", serial, "pull", "-a", src, dest, timeout=120)
    finally:
        os.umask(old)
    if not (r and r.returncode == 0):
        return ""
    os.chmod(dest, 0o600)
    return dest


def camera():
    """Continuity Camera, roughly: open the phone's camera, wait for a new
    photo, and put it on this PC's clipboard."""
    serial = ready_adb("open the phone's camera")
    if not serial:
        return 1
    before = {name for _, name in camera_photos(serial)}
    adb("-s", serial, "shell", "input keyevent KEYCODE_WAKEUP; am start -a android.media.action.STILL_IMAGE_CAMERA")
    notify("Take the photo on your phone", "It will land on this PC's clipboard.", "low")
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(1.5)
        new = sorted(p for p in camera_photos(serial) if p[1] not in before)
        if not new:
            continue
        path = pull_photo(serial, new[-1][1])
        if not path:
            break
        with open(path, "rb") as f:
            subprocess.run(["wl-copy", "--type", PHOTO_TYPES[os.path.splitext(path)[1].lower()]], stdin=f)
        notify("Photo on your clipboard", f"Also saved to {path.replace(os.path.expanduser('~'), '~')}",
               image=path, open_path=path)
        return 0
    notify("No photo came through", "Nothing new showed up in the phone's camera folder.", "low")
    return 1


def photos(quiet=False):
    """Copy camera photos taken since the last import to PHOTOS_DIR. The first
    import only reaches back three days."""
    serial, state = adb_target(eager=not quiet)
    if state != "device":
        if not quiet:
            notify("Can't import photos", "ADB isn't connected. Plug the phone in once with USB debugging on.")
        return 1
    state_path = os.path.join(STATE_DIR, "photos.json")
    try:
        with open(state_path) as f:
            since = int(json.load(f)["since"])
    except (OSError, ValueError, KeyError, TypeError):
        since = int(time.time()) - 3 * 86400
    got = []
    for mtime, name in sorted(p for p in camera_photos(serial) if p[0] > since):
        dest = os.path.join(PHOTOS_DIR, name)
        if not os.path.exists(dest):
            if not pull_photo(serial, name):
                break  # try the rest next time, from here
            got.append(dest)
        since = mtime
    private_dir(STATE_DIR)
    with open(state_path, "w") as f:
        json.dump({"since": since}, f)
    if got:
        notify(f"{len(got)} new photo{'s' if len(got) > 1 else ''} from your phone",
               f"In {PHOTOS_DIR.replace(os.path.expanduser('~'), '~')}", image=got[-1], open_path=PHOTOS_DIR)
    elif not quiet:
        notify("No new photos", "Everything on the phone's camera is already here.", "low")
    return 0


# --- Sending -------------------------------------------------------------------

def choose_files(title):
    r = subprocess.run(["omarchy-file-select", "--title", title, "--multiple"], capture_output=True, text=True)
    return [line for line in r.stdout.splitlines() if line] if r.returncode == 0 else []


def send(files):
    """AirDrop, more or less: Taildrop the files to the phone, or hand them to
    KDE Connect when the phone is in reach but its Tailscale isn't."""
    files = [os.path.abspath(f) for f in files] or choose_files("Send to phone")
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return 0
    online = [p for p in tailnet_phones().values() if p["online"] and p["dns"]]
    if online:
        return subprocess.run(["omarchy-tailscale-send", online[0]["dns"], *files]).returncode
    phones = reachable_phones()
    if phones:
        dev_id, name = phones[0]
        urls = [Gio.File.new_for_path(f).get_uri() for f in files]
        if device_call(dev_id, "/share", ".device.share", "shareUrls", args=GLib.Variant("(as)", (urls,))) == 0:
            what = os.path.basename(files[0]) if len(files) == 1 else f"{len(files)} files"
            notify(f"Sent to {name}", what, "low")
            return 0
    notify("Phone not reachable", "Neither Tailscale nor KDE Connect can see it right now.", "critical")
    return 1


def screenshot_dir():
    d = os.environ.get("OMARCHY_SCREENSHOT_DIR")
    if not d:
        r = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True, text=True)
        d = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else os.path.expanduser("~/Pictures")
    return d


def send_screenshot():
    shots = glob.glob(os.path.join(screenshot_dir(), "screenshot-*"))
    if not shots:
        notify("No screenshot to send", f"Nothing in {screenshot_dir()} yet.", "low")
        return 1
    return send([max(shots, key=os.path.getmtime)])


def clipboard_text():
    try:
        r = subprocess.run(["wl-paste", "--no-newline", "--type", "text"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def focused_class():
    r = subprocess.run(["hyprctl", "-j", "activewindow"], capture_output=True, text=True)
    try:
        return (json.loads(r.stdout).get("class") or "").lower()
    except ValueError:
        return ""


def browser_url():
    """The focused tab's address, read the way you would: address bar, copy,
    escape. Whatever text was on the clipboard goes back afterwards."""
    saved = clipboard_text()
    time.sleep(0.35)  # the shortcut's own keys need to be up first
    subprocess.run(["wtype", "-M", "ctrl", "-k", "l", "-m", "ctrl"])
    time.sleep(0.15)
    subprocess.run(["wtype", "-M", "ctrl", "-k", "c", "-m", "ctrl"])
    time.sleep(0.25)
    url = clipboard_text().strip()
    subprocess.run(["wtype", "-k", "Escape"])
    if saved and saved != url:
        # On stdin, not as an argument: wl-copy stays running to serve the clipboard,
        # and its arguments are readable by any user in the process list.
        subprocess.run(["wl-copy"], input=saved, text=True)
    return url if re.match(r"https?://\S+$", url) else ""


def handoff():
    """Handoff: the browser tab you're on opens on the phone. Anywhere else,
    a link on the clipboard opens there, and other text lands on its clipboard."""
    url = browser_url() if any(b in focused_class() for b in BROWSERS) else ""
    text = url or clipboard_text().strip()
    phones = reachable_phones()
    if not phones:
        notify("Phone not reachable", "KDE Connect can't see it right now.", "critical")
        return 1
    if not text:
        notify("Nothing to hand off", "Focus a browser tab, or copy a link or some text first.", "low")
        return 1
    dev_id, name = phones[0]
    if re.match(r"(https?|geo|tel|mailto):\S+$", text):
        rc = device_call(dev_id, "/share", ".device.share", "shareUrl", args=GLib.Variant("(s)", (text,)))
        notify(f"Opened on {name}", text, "low")
    else:
        rc = device_call(dev_id, "/share", ".device.share", "shareText", args=GLib.Variant("(s)", (text,)))
        notify(f"On {name}'s clipboard", text[:120], "low")
    return rc


# --- Phone → PC commands -----------------------------------------------------

def pc_commands():
    """The buttons the phone shows under KDE Connect's "Run command"."""
    omarchy = shutil.which("omarchy") or "omarchy"
    cmds = [
        ("Lock PC", f"{omarchy} system lock"),
        ("Night light", f"{omarchy} toggle nightlight"),
        ("Mute PC", f"{omarchy} audio output volume mute-toggle"),
        ("Silence notifications", f"{omarchy} toggle notification silencing"),
        ("Keep PC awake", f"{omarchy} toggle idle"),
        ("Mirror this phone", f"uwsm-app -- python3 {SCRIPT} mirror"),
        ("Suspend PC", "systemctl suspend"),
    ]
    if os.path.exists(VNC_HELPER):
        cmds.insert(5, ("VNC on/off", f"python3 {SCRIPT} vnc-toggle"))
    # Only offer radios this PC actually has.
    if glob.glob("/sys/class/bluetooth/hci*"):
        cmds.insert(2, ("Bluetooth on/off", f"{omarchy} bluetooth power toggle"))
    if glob.glob("/sys/class/net/*/wireless"):
        cmds.insert(2, ("Wi-Fi on/off",
                        "nmcli radio wifi $(nmcli radio wifi | grep -q enabled && echo off || echo on)"))
    return cmds


def setup_commands():
    paired = [d for d in map(read_device, device_ids()) if d["paired"]]
    if not paired:
        print("No paired phone yet", file=sys.stderr)
        return 1
    commands = {re.sub(r"\W+", "_", name.lower()): {"name": name, "command": command}
                for name, command in pc_commands()}
    # KDE Connect keeps plugin settings as QSettings ini files; the command
    # list is a JSON QByteArray, which QSettings writes as a quoted @ByteArray.
    blob = "@ByteArray(" + json.dumps(commands, separators=(",", ":")) + ")"
    blob = blob.replace("\\", "\\\\").replace('"', '\\"')
    for dev in paired:
        path = os.path.expanduser(f"~/.config/kdeconnect/{dev['id']}/kdeconnect_runcommand/config")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(f'[General]\ncommands="{blob}"\n')
        print(f"{dev['name']}: {len(commands)} commands")
    # The daemon only reads the list at startup; D-Bus activation brings it
    # back, and it owns its bus name a moment before its objects exist.
    subprocess.run(["pkill", "-x", "kdeconnectd"], check=False)
    for _ in range(20):
        time.sleep(0.5)
        try:
            device_ids()
            return 0
        except GLib.Error:
            pass
    print("KDE Connect didn't come back", file=sys.stderr)
    return 1


def vnc_toggle():
    """Run-command entry: switch VNC, then tell the phone where it ended up,
    since a run command can't show its result there."""
    subprocess.run(["python3", VNC_HELPER, "toggle"], check=False)
    r = subprocess.run(["python3", VNC_HELPER, "status"], capture_output=True, text=True)
    try:
        on = json.loads(r.stdout).get("running")
    except ValueError:
        on = None
    ping("VNC is on" if on else "VNC is off" if on is False else "VNC didn't answer")
    return 0


def lost():
    """Cut the phone off from this PC: lock it while it's still in reach,
    unpair it (which takes away its Run command buttons too), stop looking
    for it over Tailscale and drop ADB. Removing it from the tailnet itself
    needs the admin console, which this opens."""
    done = []
    serial, state = adb_target()
    if state == "device":
        r = adb("-s", serial, "shell", "input keyevent KEYCODE_SLEEP")
        if r and r.returncode == 0:
            done.append("locked its screen")
    subprocess.run(["pkill", "-x", "scrcpy"], check=False)
    adb("disconnect")
    try:
        for dev_id in device_ids():
            p = props(f"/devices/{dev_id}", IFACE + ".device") or {}
            if p.get("isPaired") and device_call(dev_id, "", ".device", "unpair") == 0:
                done.append("unpaired " + (p.get("name") or dev_id))
        custom = custom_devices()
        set_custom_devices([a for a in custom if not on_tailnet(a)])
    except GLib.Error as e:
        print(e.message, file=sys.stderr)
    names = sorted({p["name"] for p in tailnet_phones().values()}) or ["the phone"]
    subprocess.Popen(["xdg-open", TAILSCALE_ADMIN], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    notify("Phone cut off from this PC",
           ("Done: " + ", ".join(done) + ". " if done else "")
           + f"Now remove {' / '.join(names)} from your tailnet on the Tailscale page that just opened.",
           "critical")
    return 0


# --- Entry -------------------------------------------------------------------

def status():
    out = {"time": int(time.time()), "daemon": True, "devices": [], "adb": None,
           "mirroring": mirroring(), "tailnet": None, "presence": read_presence(),
           "sms": bool(shutil.which("kdeconnect-sms"))}
    try:
        out["pictures"] = kdeconnect_gate()  # before anything that could start KDE Connect
        out["devices"] = [read_device(i) for i in device_ids()]
        out["tailnet"] = read_tailnet(out["devices"])
    except Exposed as e:
        out.update(daemon=False, stopped=True, pictures=e.message)
    except GLib.Error:
        out["daemon"] = False
    out["adb"] = read_adb()
    print(json.dumps(out))
    return 0


def main(argv):
    cmd = argv[0] if argv else "status"
    args = argv[1:]
    if cmd == "status":
        return status()
    if cmd == "ring":
        return device_call(args[0], "/findmyphone", ".device.findmyphone", "ring")
    if cmd == "clipboard":
        return device_call(args[0], "/clipboard", ".device.clipboard", "sendClipboard")
    if cmd == "pair":
        return device_call(args[0], "", ".device", "requestPairing")
    if cmd == "accept":
        return device_call(args[0], "", ".device", "acceptPairing")
    if cmd == "reject":
        return device_call(args[0], "", ".device", "cancelPairing", "rejectPairing")
    if cmd == "unpair":
        return device_call(args[0], "", ".device", "unpair")
    if cmd == "dismiss":
        return device_call(args[0], f"/notifications/{args[1]}", ".device.notifications.notification", "dismiss")
    if cmd == "reply" and len(args) == 2:
        message = sys.stdin.readline().strip()
        return reply(*args, message) if message else 2
    if cmd == "toggle" and len(args) == 2 and args[0] in TOGGLES and args[1] in ("on", "off"):
        return adb_shell(TOGGLES[args[0]][args[1]])
    if cmd == "wifi-adb":
        return wifi_adb()
    if cmd == "lock-phone":
        return adb_shell("input keyevent KEYCODE_SLEEP")
    if cmd == "mirror":
        return mirror(args[0] if args and PACKAGE.fullmatch(args[0]) else None)
    if cmd == "stop-mirror":
        return subprocess.run(["pkill", "-x", "scrcpy"], check=False).returncode
    if cmd == "open-app" and len(args) == 1:
        return open_app(args[0])
    if cmd == "send":
        return send(args)
    if cmd == "send-screenshot":
        return send_screenshot()
    if cmd == "handoff":
        return handoff()
    if cmd == "camera":
        return camera()
    if cmd == "photos":
        return photos(quiet="--quiet" in args)
    if cmd == "sms":
        kdeconnect_gate()  # the SMS app starts KDE Connect too
        return subprocess.run(["uwsm-app", "--", "kdeconnect-sms"]).returncode
    if cmd == "setup-phone":
        return setup_phone()
    if cmd == "ping" and args:
        return ping(" ".join(args))
    if cmd == "vnc-toggle":
        return vnc_toggle()
    if cmd == "rescan":
        call("", IFACE + ".daemon", "forceOnNetworkChange")
        return 0
    if cmd == "setup-commands":
        return setup_commands()
    if cmd == "setup-kdeconnect":
        return setup_kdeconnect()
    if cmd == "tailnet" and (not args or args[0] in ("on", "off")):
        return tailnet(not args or args[0] == "on")
    if cmd == "lost":
        return lost()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exposed as e:
        print(e.message, file=sys.stderr)
        sys.exit(1)
