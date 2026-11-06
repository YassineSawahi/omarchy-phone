#!/usr/bin/env python3
"""The background half of the phone link (the bar widget in this repo is the
other): works out whether the phone is home, and acts on it.

  Walk-away lock  When the phone leaves, lock this PC as soon as it has gone
                  30 s without input. A VNC viewer holds that off until they
                  disconnect. Unlocking at the desk while the phone is away
                  stands it down until the phone is back.
  Welcome back    When the phone comes home, light the screen up on the lock
                  screen. Nothing unlocks by itself.
  ADB             A phone plugged in over USB gets moved to ADB over Wi-Fi.
                  A phone that joins someone else's Wi-Fi gets ADB switched
                  off, since Android 10 listens for it on every network.
  Focus           Do Not Disturb on the phone and on this PC follow each other.
  Photos          New camera photos come over to ~/Pictures/Phone while the
                  phone is home.
  Battery         Measures the battery's real capacity over one run down from
                  a full charge, and says what it found.

Home means Tailscale reaches the phone directly on this PC's own network;
reaching it any other way means away. Not reaching it at all changes
nothing, so a phone that is off or flat never locks anything.

Runs as omarchy-phone-daemon.service and logs to the journal. It never writes
into the plugin folder: any change in there reloads the bar widget.
"""

import ipaddress
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))  # phone.py sits one up
sys.dont_write_bytecode = True  # no __pycache__ in the plugin folder, for the same reason
sys.path.insert(0, PLUGIN_DIR)
import phone  # noqa: E402

TICK = 10             # seconds between rounds
PROBE_EVERY = 30      # seconds between presence checks
AWAY_CONFIRM = 2      # away readings in a row before acting on them
WIFI_CHECK_EVERY = 60
USB_SWITCH_EVERY = 600
PHOTOS_EVERY = 30 * 60

BATTERY_EVERY = 120   # seconds between battery readings during a measured run
BATTERY_TARGET = 10   # % at which a run is read off
FULL_SPAN = 70        # percentage points a run must cover to count as complete (99 % → 29 %)
MIN_SPAN = 30         # below this a run is too short to say anything
RUN_FILE = os.path.join(phone.STATE_DIR, "battery-run.json")

# The phone's Wi-Fi router, as a MAC address; nothing when it's off Wi-Fi.
PHONE_GATEWAY = ("gw=$(ip route show table all | grep -m1 'default via .* dev wlan0' | cut -d' ' -f3); "
                 "[ -n \"$gw\" ] && ip neigh show \"$gw\" dev wlan0 | grep -o 'lladdr [0-9a-f:]*' | cut -d' ' -f2")

# A battery history entry: time, (event count), then the level as three digits.
HISTORY_LEVEL = re.compile(r"\s*\S+\s+\(\d+\)\s+(\d{3})\s")


def log(*parts):
    print(*parts, flush=True)


def run(*cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def output(*cmd, timeout=10):
    r = run(*cmd, timeout=timeout)
    return r.stdout.strip() if r and r.returncode == 0 else ""


def own_networks():
    """The networks this PC is on itself, not Docker's or Tailscale's."""
    try:
        links = json.loads(output("ip", "-j", "addr") or "[]")
    except ValueError:
        return []
    nets = []
    for link in links:
        if re.match(r"(lo|docker|br-|veth|tailscale)", link.get("ifname", "")):
            continue
        for a in link.get("addr_info", []):
            try:
                nets.append(ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}").network)
            except (KeyError, ValueError):
                pass
    return nets


def where_is(ip):
    """'home', 'away' or None (no answer), from the path Tailscale takes to the phone."""
    pongs = re.findall(r"pong from .* via (\S+) in", output("tailscale", "ping", "--c", "5", "--timeout", "3s", ip,
                                                             timeout=30))
    if not pongs:
        return None
    host = pongs[-1].rsplit(":", 1)[0].strip("[]")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:  # DERP(…) or another relay
        return "away"
    return "home" if any(addr in net for net in own_networks()) else "away"


def pc_gateway_mac():
    try:
        route = next(r for r in json.loads(output("ip", "-j", "route", "show", "default") or "[]") if r.get("gateway"))
        neigh = json.loads(output("ip", "-j", "neigh", "show", route["gateway"], "dev", route["dev"]) or "[]")
        return neigh[0].get("lladdr", "") if neigh else ""
    except (StopIteration, ValueError, KeyError):
        return ""


def shell(target, method, *args):
    return output("omarchy-shell", target, method, *args)


def vnc_viewers():
    try:
        return len(json.loads(output("wayvncctl", "--json", "client-list") or "[]"))
    except ValueError:
        return 0


# --- battery ---------------------------------------------------------------
#
# The phone's fuel gauge counts the charge drawn since the last full charge
# ("Charge counter", in µAh). Divide what a stretch of the run used by the
# percentage points it covered and you have mAh per percent; a hundred of
# those is the capacity. 100 % itself is left out: the phone keeps showing it
# for a while after unplugging.

def battery_now(serial):
    """Level, mAh used since full, and whether a charger is attached."""
    r = phone.adb("-s", serial, "shell", "dumpsys battery")
    text = r.stdout if r and r.returncode == 0 else ""
    level = re.search(r"^\s*level: (\d+)", text, re.M)
    counter = re.search(r"Charge counter: (\d+)", text)
    status = re.search(r"^\s*status: (\d+)", text, re.M)
    if not (level and counter and status):
        return None
    powered = re.search(r"(AC|USB|Wireless) powered: true", text)
    return {"level": int(level.group(1)), "used": int(counter.group(1)) // 1000,
            "charging": bool(powered) or status.group(1) in ("2", "5")}  # 2 charging, 5 full


def history_points(serial):
    """{level: mAh used} where each 1 % step began during the current run, from
    Android's battery history, which starts over when the phone leaves the
    charger full. Stops where the level climbs again, i.e. at a recharge."""
    r = phone.adb("-s", serial, "shell", "dumpsys batterystats --history", timeout=90)
    points, level, lowest = {}, None, 101
    for line in r.stdout.splitlines() if r and r.returncode == 0 else []:
        m = HISTORY_LEVEL.match(line)
        if m:
            level = int(m.group(1))
            if level > lowest + 1:
                break
            lowest = min(lowest, level)
        c = re.search(r"\bcharge=(\d+)", line)
        if c and level is not None and level not in points:
            points[level] = int(c.group(1))
    return points


def design_capacity(serial):
    r = phone.adb("-s", serial, "shell", "dumpsys batterystats | grep -m1 'Capacity:'", timeout=90)
    m = re.search(r"Capacity: (\d+)", r.stdout if r else "")
    return int(m.group(1)) if m else None


def estimate(points):
    """Capacity in mAh with a ± margin, from {level: mAh used}. The margin
    combines how much single 1 % steps scatter with a 3 % allowance for the
    gauge itself and how evenly the phone spreads its percentages."""
    levels = sorted((lvl for lvl in points if lvl <= 99), reverse=True)
    if len(levels) < 5:
        return None
    top, bottom = levels[0], levels[-1]
    per_point = abs(points[bottom] - points[top]) / (top - bottom)
    steps = [abs(points[b] - points[a]) for a, b in zip(levels, levels[1:]) if a - b == 1]
    scatter = statistics.stdev(steps) / len(steps) ** 0.5 if len(steps) > 2 else per_point * 0.1
    capacity = per_point * 100
    margin = ((scatter * 100) ** 2 + (0.03 * capacity) ** 2) ** 0.5
    return {"capacity": round(capacity), "margin": round(margin), "from": top, "to": bottom}


class Daemon:
    def __init__(self):
        self.where = "unknown"
        self.since = time.time()
        self.away_readings = 0
        self.last_probe = 0
        self.armed = False        # walk-away lock waiting for the PC to go idle
        self.was_locked = False
        self.adb_closed = False   # switched off on someone else's Wi-Fi
        self.last_wifi_check = 0
        self.last_remote_connect = 0
        self.last_usb_switch = 0
        self.focus = None         # (phone, PC) Do Not Disturb as last seen
        self.photos_due = False
        self.last_photos = 0
        self.photos_proc = None
        self.last_battery = 0
        self.battery = self.load_run()
        self.written = None

    # --- presence ----------------------------------------------------------

    def phone_ip(self):
        ips = sorted(phone.tailnet_phones())
        return ips[0] if ips else None

    def check_presence(self):
        ip = self.phone_ip()
        seen = where_is(ip) if ip else None
        if seen == "home":
            self.away_readings = 0
            if self.where != "home":
                self.arrive()
        elif seen == "away":
            self.away_readings += 1
            if self.away_readings >= AWAY_CONFIRM and self.where != "away":
                self.leave()

    def arrive(self):
        before = self.where
        self.where, self.since = "home", time.time()
        self.armed = False
        log(f"phone is home (was {before})")
        if before == "away":
            if shell("lock", "isLocked") == "true":
                run("omarchy-system-wake")
            self.photos_due = True
            serial, state = phone.adb_target(eager=True)
            if state != "device" and self.adb_closed:
                phone.notify("Welcome back", "Plug the phone into this PC once to switch ADB back on "
                             "for mirroring, Focus sync and photos.", "low")

    def leave(self):
        before = self.where
        self.where, self.since = "away", time.time()
        self.armed = True
        self.was_locked = shell("lock", "isLocked") == "true"
        log(f"phone left (was {before}); walk-away lock armed")

    # --- walk-away lock ----------------------------------------------------

    def walkaway(self):
        if self.where != "away":
            return
        locked = shell("lock", "isLocked") == "true"
        viewers = vnc_viewers()
        if self.was_locked and not locked and not viewers and self.armed:
            self.armed = False
            log("unlocked at the desk while the phone is away; standing down until it's back")
        self.was_locked = locked
        # The bar widget keeps a 30 s idle monitor; no answer counts as idle.
        if self.armed and not locked and not viewers and shell("io.github.yassinesawahi.phone", "idle") != "false":
            log("phone away and PC idle: locking")
            run("omarchy", "system", "lock")

    # --- ADB ---------------------------------------------------------------

    def adb_upkeep(self):
        """Serial of a working ADB link to the phone, or None."""
        now = time.time()
        if self.where == "away":
            serial = None if self.adb_closed else self.tailnet_serial()
            if serial and now - self.last_wifi_check > WIFI_CHECK_EVERY:
                self.last_wifi_check = now
                if self.close_on_foreign_wifi(serial):
                    serial = None
            if not serial:
                self.focus = None
            return serial
        serial, state = phone.adb_target(eager=False)
        if state != "device":
            self.focus = None
            return None
        if not phone.is_wireless(serial):
            if now - self.last_usb_switch > USB_SWITCH_EVERY:
                self.last_usb_switch = now
                log("phone on USB: moving ADB to Wi-Fi")
                if phone.wifi_adb() == 0:
                    self.adb_closed = False
            return None
        self.adb_closed = False
        return serial

    def tailnet_serial(self):
        """ADB through Tailscale, which reaches the phone wherever it is. The
        link it had at home lingers in `adb devices` after it leaves, and every
        command on it would hang until the timeout, so that one goes."""
        ip = self.phone_ip()
        if not ip:
            return None
        serial = f"{ip}:5555"
        devices = phone.adb_devices()
        for other, _ in devices:
            if other != serial and ":" in other:
                phone.adb("disconnect", other)
        if (serial, "device") in devices:
            return serial
        if time.time() - self.last_remote_connect < WIFI_CHECK_EVERY:
            return None
        self.last_remote_connect = time.time()
        r = phone.adb("connect", serial)
        return serial if r and "connected to" in r.stdout else None

    def close_on_foreign_wifi(self, serial):
        r = phone.adb("-s", serial, "shell", PHONE_GATEWAY)
        router = r.stdout.strip() if r and r.returncode == 0 else ""
        home = pc_gateway_mac()
        if not router or not home or router == home:
            return False  # on mobile data, on the home Wi-Fi, or can't tell
        log(f"phone joined another Wi-Fi (router {router}); switching its ADB off")
        phone.adb("-s", serial, "usb")
        phone.adb("disconnect")
        self.adb_closed = True
        self.focus = None
        phone.notify("ADB switched off on your phone",
                     "It joined a Wi-Fi that isn't yours. Plug it into this PC once at home to switch it back on.",
                     "low")
        return True

    # --- Focus -------------------------------------------------------------

    def focus_sync(self, serial):
        r = phone.adb("-s", serial, "shell", "settings get global zen_mode")
        mode = r.stdout.strip() if r and r.returncode == 0 else ""
        pc = shell("notifications", "isDnd")
        if not mode.isdigit() or pc not in ("on", "off"):
            return
        phone_on, pc_on = mode != "0", pc == "on"
        if self.focus is not None:
            was_phone, was_pc = self.focus
            if phone_on != was_phone and pc_on == was_pc and pc_on != phone_on:
                shell("notifications", "setDnd", "on" if phone_on else "off")
                run("omarchy-shell", "-q", "omarchy.indicators", "refresh")
                pc_on = phone_on
                log(f"Do Not Disturb {'on' if phone_on else 'off'}: phone → PC")
            elif pc_on != was_pc and phone_on == was_phone and phone_on != pc_on:
                phone.adb("-s", serial, "shell", f"cmd notification set_dnd {'on' if pc_on else 'off'}")
                phone_on = pc_on
                log(f"Do Not Disturb {'on' if pc_on else 'off'}: PC → phone")
        self.focus = (phone_on, pc_on)

    # --- photos ------------------------------------------------------------

    def photos_upkeep(self):
        if self.photos_proc and self.photos_proc.poll() is None:
            return
        now = time.time()
        if self.where == "home" and (self.photos_due or now - self.last_photos > PHOTOS_EVERY):
            self.photos_due, self.last_photos = False, now
            self.photos_proc = subprocess.Popen(["python3", phone.SCRIPT, "photos", "--quiet"])

    # --- battery -----------------------------------------------------------

    def load_run(self):
        try:
            with open(RUN_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_run(self):
        os.makedirs(phone.STATE_DIR, mode=0o700, exist_ok=True)
        tmp = RUN_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.battery, f)
        os.replace(tmp, RUN_FILE)

    def battery_upkeep(self, serial):
        """Follow one run from a full charge down to BATTERY_TARGET %. Readings
        every couple of minutes back up Android's own history, in case that
        overflows before the end."""
        run_ = self.battery
        if run_.get("state") == "done" or time.time() - self.last_battery < BATTERY_EVERY:
            return
        self.last_battery = time.time()
        now = battery_now(serial)
        if not now:
            return
        if not run_.get("state"):
            # First look: maybe a run from a full charge is already under way.
            measuring = not now["charging"] and 100 in history_points(serial)
            run_.update(state="measuring" if measuring else "waiting", samples=[])
            log(f"battery: {'measuring the run under way' if measuring else 'waiting for a full charge'}")
        if now["charging"]:
            if run_["state"] == "measuring":
                self.read_off(serial)
        elif run_["state"] == "waiting" and now["level"] >= 100:
            run_.update(state="measuring", samples=[])
            log("battery: measuring this run from full")
        elif run_["state"] == "measuring":
            run_["samples"].append([int(time.time()), now["level"], now["used"]])
            if now["level"] <= BATTERY_TARGET:
                self.read_off(serial)
        self.save_run()

    def read_off(self, serial):
        """Work out the capacity for the run that just ended, and tell the user."""
        run_ = self.battery
        points = history_points(serial)
        for _, level, used in run_.get("samples", []):
            points.setdefault(level, used)  # fills in whatever the history lost
        result = estimate(points)
        if not result or result["from"] - result["to"] < MIN_SPAN:
            log("battery: run too short to measure; waiting for the next full charge")
            run_.update(state="waiting", samples=[])
            return
        design = design_capacity(serial) or 4000
        complete = result["from"] - result["to"] >= FULL_SPAN
        result.update(at=int(time.time()), design=design, complete=complete)
        run_.setdefault("results", []).append(result)
        run_.update(state="done" if complete else "waiting", samples=[])
        # Shown to the nearest 10 mAh: the margin is ±100 or so, more digits would be noise.
        cap, margin = round(result["capacity"], -1), max(10, round(result["margin"], -1))
        health = 100 * result["capacity"] / design
        if complete:
            phone.notify(f"Battery: about {cap:,} mAh ({health:.0f}% of new)",
                         f"Measured from {result['from']}% down to {result['to']}%, give or take {margin:,} mAh. "
                         f"New, it held {design:,} mAh.")
        else:
            phone.notify(f"Battery: roughly {cap:,} mAh ({health:.0f}% of new)",
                         f"It went on the charger at {result['to']}%, so this is only good to ±{margin:,} mAh. "
                         "I'll measure again the next time it runs down from a full charge.")
        log(f"battery: {cap} mAh ±{margin} ({result['from']}%→{result['to']}%), design {design}, "
            f"{'complete' if complete else 'partial'}")

    # --- loop --------------------------------------------------------------

    def write_state(self):
        state = {"where": self.where, "since": int(self.since), "armed": self.armed, "adbClosed": self.adb_closed}
        if state != self.written:
            tmp = phone.PRESENCE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, phone.PRESENCE_FILE)
            self.written = state

    def round(self):
        now = time.time()
        if now - self.last_probe >= PROBE_EVERY:
            self.last_probe = now
            self.check_presence()
        self.walkaway()
        serial = self.adb_upkeep()
        if serial:
            self.focus_sync(serial)
            self.photos_upkeep()
            self.battery_upkeep(serial)
        self.write_state()

    def run(self):
        log("phone daemon started")
        while True:
            try:
                self.round()
            except Exception as e:  # keep going; one bad round shouldn't end the service
                log(f"round failed: {e!r}")
            time.sleep(TICK)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Daemon().run()
