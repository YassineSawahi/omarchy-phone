# Phone for Omarchy

An [Omarchy](https://omarchy.org) bar widget for your Android phone, at home or away. It shows battery and notifications, rings the phone, flips the phone's toggles, mirrors its screen and sends files. It also has a lost-phone switch. It's built on [KDE Connect](https://kdeconnect.kde.org) over Wi-Fi and [Tailscale](https://tailscale.com), plus wireless ADB.

## What it does

- **Bar:** the phone's battery. Middle-click rings it; drop files on the icon to send them.
- **Panel** (left click):
  - Phone card: name, link (home Wi-Fi or Tailscale), signal, battery.
  - Ring, mirror (scrcpy), send files, camera (the photo lands on this PC's clipboard), clipboard to phone, import new photos to `~/Pictures/Phone`, SMS, lock the phone.
  - Phone-side switches: Bluetooth, Do Not Disturb, mobile data, location (through ADB).
  - The phone's latest notifications: reply inline or dismiss.
  - Pairing requests, and a **Lost phone…** button that locks the phone if it's still in reach, unpairs it, drops ADB, and opens the Tailscale admin page so you can remove it from your tailnet.
- **Away from home:** `phone.py tailnet on` gives KDE Connect the phone's Tailscale address, so it stays connected over Tailscale. Files go over Taildrop when the phone is online there.
- **Phone → PC:** `phone.py setup-commands` gives the phone KDE Connect "Run command" buttons for this PC (it replaces any Run command list the phone had, and only when you run it): lock, night light, mute, silence notifications, keep awake, mirror, suspend, Wi-Fi/Bluetooth, and VNC on/off if [omarchy-vnc](https://github.com/YassineSawahi/omarchy-vnc) is installed.

## Install

```bash
omarchy plugin add https://github.com/YassineSawahi/omarchy-phone.git --enable
```

Needs `kdeconnect` and `python-gobject`. KDE Connect has to be installed on the phone too, on the same Wi-Fi the first time, to pair. For the phone-side switches, mirroring and the camera: `android-tools` (adb) and `scrcpy`, with USB debugging on (or Wireless debugging paired with `adb pair` on Android 11+). Optional extras: `tailscale` for away-from-home, and `wtype` + `wl-clipboard` for handoff.

`phone.py setup-phone` grants KDE Connect on the phone the permissions it needs for the clipboard, calls and SMS (needs ADB).

`phone.py setup-kdeconnect` (the panel's **Keep private** button) keeps the pictures in your phone's notifications out of the shared `/tmp`: see [Privacy](#privacy).

## Controls

| Action | Does |
|---|---|
| Left click | Open or close the panel |
| Middle click | Ring the phone |
| Right click | Read the state again |
| Drop files on the icon | Send them to the phone |
| `omarchy-shell io.github.yassinesawahi.phone <fn>` | `toggle`, `ring`, `mirror`, `sendFiles`, `refresh`, `close` |

Every panel action is also a `phone.py` subcommand (`ring`, `mirror`, `send`, `handoff`, `camera`, `photos`, `lost`, …). Run `python3 phone.py help` for the full list.

Settings: refresh interval (default 20 s), low battery colour (default 15%), notifications shown (default 5).

## Optional: the background daemon

[`daemon/phoned.py`](daemon/phoned.py) works out whether the phone is home and acts on it:

- **Walk-away lock:** when the phone leaves, this PC locks as soon as it has gone 30 s without input. A VNC viewer holds that off until they disconnect, and unlocking at the desk while the phone is away stands it down until the phone is back.
- **Welcome back:** when the phone comes home, the screen lights up on the lock screen. Nothing unlocks by itself.
- **ADB upkeep:** a phone plugged in over USB gets moved to ADB over Wi-Fi. A phone that joins someone else's Wi-Fi gets ADB switched off, since Android 10 listens for it on every network.
- **Focus:** Do Not Disturb on the phone and on this PC follow each other.
- **Photos:** new camera photos come over to `~/Pictures/Phone` while the phone is home.
- **Battery health:** it measures the battery's real capacity over one run down from a full charge and tells you what it found.

"Home" means Tailscale reaches the phone directly on this PC's own network. Not reaching it at all changes nothing, so a phone that's off or flat never locks anything. The panel shows home or away from the daemon. Without it, the phone counts as home while KDE Connect reaches it over the LAN.

```bash
cp ~/.config/omarchy/plugins/io.github.yassinesawahi.phone/daemon/omarchy-phone-daemon.service ~/.config/systemd/user/
systemctl --user enable --now omarchy-phone-daemon
journalctl --user -u omarchy-phone-daemon -f    # what it's doing
```

## How it works

The widget runs `phone.py` on a timer and draws the JSON it prints. Every button runs a `phone.py` subcommand. The helper reaches your phone three ways:

- **KDE Connect** is the main link: `kdeconnectd` on this PC, the KDE Connect app on the phone. You pair them once on the same Wi-Fi. It carries the battery, notifications and inline replies, ringing, the clipboard, links, SMS, pairing, and the phone's Run command buttons for this PC. `phone.py` talks to `kdeconnectd` over D-Bus on your session bus.
- **ADB** (wireless debugging) does what Android won't let an ordinary app do: the Bluetooth, Do Not Disturb, data and location switches, locking the phone, scrcpy mirroring, the camera, photo import and `setup-phone`.
- **Taildrop** sends files whenever the phone is online on your tailnet. When it isn't, but KDE Connect can see the phone, they go through KDE Connect instead.

**Home and away.** At home, KDE Connect finds the phone on your Wi-Fi by itself, and so does ADB. On mobile data or someone else's Wi-Fi they can't see it. `phone.py tailnet on` adds the phone's Tailscale address to KDE Connect's list of devices to try, so it reaches the phone through Tailscale wherever it is. Tailscale links your own devices directly, so nothing is opened on your router or to the internet. ADB can follow the phone over Tailscale too, but only on port 5555 (after `wifi-adb`). Wireless debugging on Android 11+ is found on the local network only.

**The daemon** asks `tailscale ping` how it reaches the phone. A direct path on this PC's own network means home. A relayed path, or a direct one from another network, means away. It writes the answer to `$XDG_RUNTIME_DIR` for the panel, and acts on it as listed above.

**Phone → PC.** The Run command buttons live in KDE Connect's settings on this PC. Tapping one on the phone asks `kdeconnectd` to run it here: an `omarchy` command, or a `phone.py` subcommand like `mirror`.

```text
 YOUR PHONE                           THIS PC
 KDE Connect app <-- home Wi-Fi ----> kdeconnectd <-- D-Bus --+
                 <-- or Tailscale -->                         |
 Wireless debug  <-- ADB -----------> adb, scrcpy <-----------+-- phone.py
 Tailscale app   <-- Taildrop ------- tailscale <-------------+      ^
                                                                     | JSON
                                      daemon (optional):         bar widget
                                      tailscale ping -> home or away
```

## Privacy

Everything stays between your PC and your phone: KDE Connect, ADB and Tailscale talk directly to each other. Notification text from the phone is shown as plain text only. On the PC, other accounts can't read or use any of it:

- Inline replies reach `phone.py` on stdin, and the clipboard text Handoff puts back goes to `wl-copy` on stdin. Notifications go to your notification service over D-Bus. None of it sits on a command line, where any account could read it in the process list.
- adb normally runs its server on `127.0.0.1:5037`, which any account on the PC could use to drive your phone. This plugin's adb (and scrcpy) use a socket in your private runtime folder instead, `$XDG_RUNTIME_DIR/omarchy-phone-adb.sock`. To share that server from your own shell: `export ADB_SERVER_SOCKET=localfilesystem:$XDG_RUNTIME_DIR/omarchy-phone-adb.sock`. If you already set `ADB_SERVER_SOCKET` yourself, the plugin uses yours. After updating, `adb kill-server` stops an old server still on port 5037.
- KDE Connect saves the pictures in your phone's notifications in `$TMPDIR/kdeconnect_$USER`, by default in the shared `/tmp`, and runs with umask `022`. If another account creates that folder first, KDE Connect uses it anyway and the pictures land there readable by that account. `setup-kdeconnect` (or **Keep private** in the panel, which shows until it's done) gives KDE Connect your private runtime folder as its temporary folder and umask `077`, for both ways it starts: at login (a drop-in for `app-org.kde.kdeconnect.daemon@autostart.service`) and when something calls it over D-Bus (`~/.local/share/dbus-1/services/org.kde.kdeconnect.service` starts that same unit). It restarts KDE Connect and deletes the pictures it left in the old folder. Until then, before anything the plugin does could start or read KDE Connect, it claims `/tmp/kdeconnect_$USER` for you (`0700`) whenever nobody else has. If another account owns that folder, the plugin doesn't start KDE Connect, stops it if it's running (with a notification), and leaves it stopped until you press **Keep private**.
- Imported photos land in `~/Pictures/Phone`, readable by you alone: the folder is set to `0700` on every import, including one an older version left open, and each photo is pulled under umask `077`, so it is `0600` from its first byte. The state folder is kept `0700` the same way. State and runtime files stay in your own folders, never `/tmp`.

## Remove

Undo the optional extras first, if you set them up:

```bash
P=~/.config/omarchy/plugins/io.github.yassinesawahi.phone
python3 $P/phone.py tailnet off                       # KDE Connect forgets the phone's Tailscale address
systemctl --user disable --now omarchy-phone-daemon   # the background daemon
rm ~/.config/systemd/user/omarchy-phone-daemon.service
rm ~/.config/systemd/user/app-org.kde.kdeconnect.daemon@autostart.service.d/omarchy-phone-private.conf \
   ~/.local/share/dbus-1/services/org.kde.kdeconnect.service   # setup-kdeconnect; KDE Connect is fine either way
```

Then remove the plugin and its state:

```bash
omarchy plugin remove io.github.yassinesawahi.phone
rm -rf ~/.local/state/omarchy-phone
```

The phone's Run command buttons from `setup-commands` would now point at nothing: clear them in KDE Connect's Run command settings, or delete `~/.config/kdeconnect/*/kdeconnect_runcommand/config`. Permissions granted with `setup-phone` stay on the phone until you revoke them in Android's app settings. Photos in `~/Pictures/Phone` are yours and stay.

## License

MIT
