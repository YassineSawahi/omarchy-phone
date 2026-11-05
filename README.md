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
- **Phone → PC:** `phone.py setup-commands` gives the phone KDE Connect "Run command" buttons for this PC: lock, night light, mute, silence notifications, keep awake, mirror, suspend, Wi-Fi/Bluetooth, and VNC on/off if [omarchy-vnc](https://github.com/YassineSawahi/omarchy-vnc) is installed.

## Install

```bash
omarchy plugin add https://github.com/YassineSawahi/omarchy-phone.git --enable
```

Needs `kdeconnect` and `python-gobject`. KDE Connect has to be installed on the phone too, on the same Wi-Fi the first time, to pair. For the phone-side switches, mirroring and the camera: `android-tools` (adb) and `scrcpy`, with USB debugging on (or Wireless debugging paired with `adb pair` on Android 11+). Optional extras: `tailscale` for away-from-home, and `wtype` + `wl-clipboard` for handoff.

`phone.py setup-phone` grants KDE Connect on the phone the permissions it needs for the clipboard, calls and SMS (needs ADB).

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

## Privacy

Everything stays between your PC and your phone: KDE Connect, ADB and Tailscale talk directly to each other. Notification text from the phone is shown as plain text only.

## License

MIT
