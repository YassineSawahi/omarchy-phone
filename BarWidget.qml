import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.yassinesawahi.phone"

  // The helper sits next to this file, wherever the plugin is installed.
  readonly property string scriptPath: decodeURIComponent(Qt.resolvedUrl("phone.py").toString().replace(/^file:\/\//, ""))
  readonly property int refreshIntervalSec: Math.max(5, Number(root.settings.refreshIntervalSec) || 20)
  readonly property int lowThreshold: Number(root.settings.lowThreshold) || 15
  readonly property int maxNotifications: root.settings.maxNotifications === undefined ? 5 : Number(root.settings.maxNotifications)

  readonly property var glyphs: ({
    phone: String.fromCodePoint(0xF011C),      // nf-md-cellphone
    charging: String.fromCodePoint(0xF140B),   // nf-md-lightning_bolt
    ring: String.fromCodePoint(0xF009E),       // nf-md-bell_ring
    mirror: String.fromCodePoint(0xF0118),     // nf-md-cast
    clipboard: String.fromCodePoint(0xF0192),  // nf-md-content_paste
    lock: String.fromCodePoint(0xF033E),       // nf-md-lock
    pair: String.fromCodePoint(0xF0337),       // nf-md-link
    close: String.fromCodePoint(0xF0156),      // nf-md-close
    bluetooth: String.fromCodePoint(0xF00AF),  // nf-md-bluetooth
    dnd: String.fromCodePoint(0xF009B),        // nf-md-bell_off
    data: String.fromCodePoint(0xF08BF),       // nf-md-signal_cellular_3
    location: String.fromCodePoint(0xF034E),   // nf-md-map_marker
    alert: String.fromCodePoint(0xF0026),      // nf-md-alert
    send: String.fromCodePoint(0xF048A),       // nf-md-send
    camera: String.fromCodePoint(0xF0100),     // nf-md-camera
    photos: String.fromCodePoint(0xF02F9),     // nf-md-image_multiple
    messages: String.fromCodePoint(0xF0369),   // nf-md-message_text
    reply: String.fromCodePoint(0xF045A)       // nf-md-reply
  })

  // Phone-side switches, in panel order. Only the ones the phone reports a
  // value for get a tile (no SIM → no mobile data tile).
  readonly property var toggleDefs: [
    { key: "bluetooth", label: "Bluetooth" },
    { key: "dnd", label: "Do Not Disturb" },
    { key: "data", label: "Mobile data" },
    { key: "location", label: "Location" }
  ]

  // Latest `phone.py status` reading; null until the first one lands.
  property var status: null
  property bool popupOpen: false
  property bool refreshQueued: false
  property date nowTick: new Date()

  // "Lost phone" takes a second click within a few seconds to go through.
  property bool lostArmed: false

  // Id of the phone notification being answered inline, if any.
  property string replyTarget: ""

  // Toggle values clicked but not yet confirmed by a reading, so tiles flip
  // straight away instead of after the adb round trip.
  property var optimistic: ({})

  readonly property var devices: root.status ? root.status.devices || [] : []

  // The phone the bar follows: the first paired one in reach, else the first paired.
  readonly property var phone: {
    var fallback = null
    for (var i = 0; i < root.devices.length; i++) {
      var d = root.devices[i]
      if (!d.paired) continue
      if (d.reachable) return d
      if (!fallback) fallback = d
    }
    return fallback
  }

  readonly property bool online: !!root.phone && root.phone.reachable
  // Taildrop only needs Tailscale, so files can go even when KDE Connect can't see the phone.
  readonly property bool canSend: root.online || (!!root.status && !!root.status.tailnet && !!root.status.tailnet.phoneOnline)
  // Home or away, from the optional presence daemon (see phone.py).
  readonly property string place: root.status && root.status.presence && root.status.presence.where !== "unknown"
    ? root.status.presence.where : ""
  readonly property var adbInfo: root.status ? root.status.adb : null
  readonly property bool adbReady: !!root.adbInfo && root.adbInfo.state === "device"
  readonly property bool mirroring: !!root.status && !!root.status.mirroring
  readonly property var pairable: root.devices.filter(function(d) { return !d.paired && d.reachable })
  readonly property var pairRequests: root.devices.filter(function(d) { return d.pairRequestedByPeer })
  readonly property bool hasLevel: root.online && root.phone.battery !== null && root.phone.battery !== undefined
  readonly property bool low: root.hasLevel && root.phone.battery <= root.lowThreshold && !root.phone.charging

  readonly property var toggleTiles: {
    if (!root.adbReady) return []
    var tiles = []
    for (var i = 0; i < root.toggleDefs.length; i++) {
      var def = root.toggleDefs[i]
      var value = root.optimistic[def.key] !== undefined ? root.optimistic[def.key] : root.adbInfo.toggles[def.key]
      if (value === undefined) continue
      tiles.push({ key: def.key, label: def.label, on: value })
    }
    return tiles
  }

  readonly property var notifications: root.online && root.maxNotifications > 0
    ? root.phone.notifications.slice(0, root.maxNotifications)
    : []

  readonly property string tooltipLabel: {
    if (!root.status) return "Looking for your phone…"
    if (root.status.stopped) return "KDE Connect stopped · Keep private, in the panel"
    if (!root.status.daemon) return "KDE Connect isn't running"
    if (root.pairRequests.length) return "Pairing request from " + root.pairRequests[0].name
    if (!root.phone) return "No phone paired · click to set up"
    if (!root.online) return root.phone.name + " · not reachable"
    var s = root.phone.name
    if (root.hasLevel) s += " · " + root.phone.battery + "%" + (root.phone.charging ? " charging" : "")
    if (root.phone.network) s += " · " + root.phone.network
    return s + "\nMiddle-click to ring it"
  }

  // Shown in place of the phone card while there is nothing to show.
  readonly property string setupHint: {
    if (!root.status) return "Looking for your phone…"
    if (root.status.stopped) return ""  // the pictures hint below says why, with the way out
    if (!root.status.daemon) return "KDE Connect isn't running. Right-click the bar icon to try again."
    if (root.phone || root.pairable.length) return ""
    return "Install KDE Connect on the phone (Play Store or F-Droid) and open it on the same Wi-Fi. It shows up here to pair."
  }

  // Set while the pictures in the phone's notifications could be read by another account on this PC.
  readonly property string picturesHint: root.status && root.status.pictures ? root.status.pictures : ""

  readonly property bool adbOverUsb: root.adbReady && !root.adbInfo.wireless

  readonly property string adbHint: {
    if (root.adbOverUsb) return "Connected over USB. Press ADB over Wi-Fi to keep it working unplugged, until the phone reboots."
    if (root.adbReady || !root.online) return ""
    if (root.adbInfo && root.adbInfo.state === "unauthorized") return "Allow debugging on the phone's prompt to finish connecting ADB."
    if (root.adbInfo) return "ADB link is " + root.adbInfo.state + "."
    return "To mirror the phone and flip its Bluetooth, Do Not Disturb, data and location: turn on USB debugging (Developer options) and plug it in once. Android 11+ can use Wireless debugging instead."
  }

  readonly property real openPanelIndicatorWidth: content.implicitWidth

  function signalBars(n) {
    return n > 0 ? "▂▄▆█".substring(0, n) : "no signal"
  }

  // Why the paired phone is out of reach, as far as KDE Connect and Tailscale can tell.
  readonly property string unreachableLine: {
    var t = root.status ? root.status.tailnet : null
    if (!t || !t.enabled) return "Not reachable · is KDE Connect open on the same Wi-Fi?"
    if (t.phoneOnline) return "On Tailscale, but KDE Connect isn't answering · open it on the phone"
    if (t.phoneOnline === false) return "Not reachable · the phone is offline or Tailscale is off on it"
    return "Not reachable"
  }

  function linkLine(d) {
    if (!d.reachable) return root.unreachableLine
    var parts = [d.charging ? "Charging" : "Connected"]
    var where = root.place || (d.via === "lan" ? "home" : "")
    if (where === "home") parts.push("at home")
    else if (where === "away") parts.push("away, via Tailscale")
    if (d.network) parts.push(d.network + (d.signal !== null ? " " + root.signalBars(d.signal) : ""))
    if (root.adbReady) parts.push("ADB")
    if (root.mirroring) parts.push("Mirroring")
    return parts.join(" · ")
  }

  function pairLine(d) {
    var key = d.verificationKey ? " · key " + d.verificationKey : ""
    if (d.pairRequestedByPeer) return "Wants to pair" + key
    if (d.pairRequested) return "Accept on the phone" + key
    return d.type ? root.capitalize(d.type) + " · not paired" : "Not paired"
  }

  function capitalize(s) {
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : ""
  }

  // Shell tooltips and labels may render rich text, and names here come from
  // other devices. Swap angle brackets for look-alikes so no name can carry markup.
  function plain(s) {
    return String(s === undefined || s === null ? "" : s).replace(/</g, "\u2039").replace(/>/g, "\u203a")
  }

  function refresh() {
    if (readProc.running) root.refreshQueued = true
    else readProc.running = true
  }

  function applyReading(text) {
    var data
    try { data = JSON.parse(text) } catch (e) { return }
    root.status = data
    if (!actionProc.running) root.optimistic = ({})
  }

  // Runs `phone.py <args…>` and re-reads once it finishes. One action at a
  // time; clicks while one is in flight are dropped.
  function run(args) {
    if (actionProc.running) return
    actionProc.command = ["python3", root.scriptPath].concat(args)
    actionProc.running = true
  }

  function ring() {
    if (root.online) root.run(["ring", root.phone.id])
  }

  function toggleMirror() {
    if (root.mirroring) {
      root.run(["stop-mirror"])
      return
    }
    // uwsm-app gives scrcpy its own scope, like any app launched from the menu.
    Quickshell.execDetached(["uwsm-app", "--", "python3", root.scriptPath, "mirror"])
    mirrorRefresh.restart()
  }

  function setToggle(key, on) {
    var next = Object.assign({}, root.optimistic)
    next[key] = on
    root.optimistic = next
    root.run(["toggle", key, on ? "on" : "off"])
  }

  // Contract KeyboardPanel expects on its `owner`: called when a click
  // outside dismisses the popup, so the `open` binding below stays intact.
  function close() {
    root.popupOpen = false
    root.lostArmed = false
  }

  function toggle() {
    root.nowTick = new Date()
    root.popupOpen = !root.popupOpen
    root.lostArmed = false
    if (root.popupOpen) root.refresh()
  }

  function reportLost() {
    if (!root.lostArmed) {
      root.lostArmed = true
      lostDisarm.restart()
      return
    }
    root.lostArmed = false
    root.run(["lost"])
  }

  // Actions that open windows or wait on the phone (file chooser, camera,
  // scrcpy) run on their own, so the panel stays usable meanwhile.
  function launch(args, closePanel) {
    Quickshell.execDetached(["uwsm-app", "--", "python3", root.scriptPath].concat(args))
    if (closePanel) root.close()
    mirrorRefresh.restart()
  }

  function sendFiles(paths) {
    root.launch(["send"].concat(paths || []), true)
  }

  // Shows the app behind a notification in the mirror window.
  function openApp(pkg) {
    root.launch(["open-app", pkg], true)
  }

  function startReply(id) {
    root.replyTarget = root.replyTarget === id ? "" : id
  }

  // Replies queue up and go out one at a time through replyProc, which hands
  // phone.py the text on stdin: on the command line, any other user on the
  // machine could read it in the process list.
  property var replyQueue: []
  property bool replyBusy: false

  function sendReply(replyId, text) {
    var message = String(text || "").replace(/[\r\n]+/g, " ").trim()
    if (!message || !root.phone) return
    root.replyTarget = ""
    root.replyQueue = root.replyQueue.concat([{ device: root.phone.id, replyId: replyId, message: message }])
    root.nextReply()
  }

  function nextReply() {
    if (root.replyBusy || root.replyQueue.length === 0) return
    root.replyBusy = true
    var next = root.replyQueue[0]
    root.replyQueue = root.replyQueue.slice(1)
    replyProc.message = next.message
    replyProc.command = ["python3", root.scriptPath, "reply", next.device, next.replyId]
    replyProc.running = true
  }

  // The floating bar can mount a hidden second copy of a widget, and an IPC
  // target goes to whichever copy registered last. Only a copy that is on
  // screen takes it, so the shortcuts open the panel you can see.
  readonly property bool onScreen: {
    var w = button.QsWindow.window
    return !!w && w.visible && root.visible && root.width > 0 && root.height > 0
  }

  // `omarchy-shell io.github.yassinesawahi.phone <fn>` — bindable to keys.
  IpcHandler {
    target: "io.github.yassinesawahi.phone"
    enabled: root.onScreen

    function toggle() { root.toggle() }
    function close() { root.close() }
    function refresh() { root.refresh() }
    function ring() { root.ring() }
    function mirror() { root.toggleMirror() }
    function sendFiles() { root.sendFiles([]) }
    // For an optional presence daemon's walk-away lock: has this PC gone 30 s without input?
    function idle(): string { return idleWatch.isIdle ? "true" : "false" }
  }

  IdleMonitor {
    id: idleWatch
    timeout: 30
    respectInhibitors: false
  }

  Process {
    id: readProc
    command: ["python3", root.scriptPath]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyReading(text)
    }
    onExited: {
      if (!root.refreshQueued) return
      root.refreshQueued = false
      Qt.callLater(root.refresh)
    }
  }

  Process {
    id: actionProc
    onExited: root.refresh()
  }

  Process {
    id: replyProc
    property string message: ""
    stdinEnabled: true
    onStarted: {
      write(message + "\n")
      message = ""
    }
    onExited: {
      root.replyBusy = false
      root.refresh()
      root.nextReply()
    }
  }

  // scrcpy takes a moment to come up; re-read so the button flips to Stop.
  Timer {
    id: mirrorRefresh
    interval: 2500
    onTriggered: root.refresh()
  }

  Timer {
    id: lostDisarm
    interval: 4000
    onTriggered: root.lostArmed = false
  }

  // Polls faster while the popup is open so toggles and notifications feel live.
  Timer {
    interval: root.popupOpen ? 5000 : root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    interval: 30000
    running: root.popupOpen
    repeat: true
    onTriggered: root.nowTick = new Date()
  }

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    tooltipText: root.plain(root.tooltipLabel)
    fixedWidth: root.vertical ? -1 : content.implicitWidth + button.scaledHorizontalMargin * 2
    fixedHeight: root.vertical ? Style.bar.iconSlot : -1

    onPressed: function(b) {
      if (b === Qt.RightButton) root.refresh()
      else if (b === Qt.MiddleButton) root.ring()
      else root.toggle()
    }

    // AirDrop-ish: drop files on the phone icon to send them.
    DropArea {
      id: dropZone
      anchors.fill: parent
      keys: ["text/uri-list"]
      onDropped: function(drop) {
        var files = []
        for (var i = 0; i < drop.urls.length; i++) {
          var url = String(drop.urls[i])
          if (url.indexOf("file://") === 0) files.push(decodeURIComponent(url.substring(7)))
        }
        if (files.length) root.sendFiles(files)
        drop.accept()
      }
    }

    Row {
      id: content
      anchors.centerIn: parent
      spacing: Style.space(4)

      readonly property color tint: dropZone.containsDrag ? Color.accent
        : root.pairRequests.length || root.low ? root.bar.urgent : button.foreground

      OpticalGlyph {
        anchors.verticalCenter: parent.verticalCenter
        width: Style.bar.iconCanvas
        height: Style.bar.iconCanvas
        text: root.glyphs.phone
        fontFamily: button.fontFamily
        fontSize: Style.bar.iconFont
        color: content.tint
        opacity: root.online || root.pairRequests.length || dropZone.containsDrag ? 1 : 0.5
      }

      // A vertical bar has no room for the percentage; it stays in the tooltip.
      Text {
        anchors.verticalCenter: parent.verticalCenter
        visible: !root.vertical && root.hasLevel
        textFormat: Text.PlainText
        text: root.hasLevel ? (root.phone.charging ? root.glyphs.charging : "") + root.phone.battery + "%" : ""
        color: content.tint
        font.family: button.fontFamily
        font.pixelSize: Style.font.body
        renderType: Text.NativeRendering
      }
    }
  }

  KeyboardPanel {
    id: popup
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.popupOpen
    focusTarget: keyCatcher
    contentWidth: popup.fittedContentWidth(Style.space(340))
    contentHeight: popup.fittedContentHeight(col.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      Column {
        id: col
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        readonly property color dim: Qt.darker(root.bar.foreground, 1.4)

        PanelSectionHeader {
          text: "PHONE"
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
        }

        Text {
          visible: root.setupHint !== ""
          width: parent.width
          textFormat: Text.PlainText
          text: root.setupHint
          color: col.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
          wrapMode: Text.WordWrap
        }

        // KDE Connect keeps notification pictures in the shared /tmp until it's set up otherwise;
        // stopped, while another account owns its folder there.
        Column {
          visible: root.picturesHint !== ""
          width: parent.width
          spacing: Style.space(6)
          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.picturesHint
            color: root.status && root.status.stopped ? root.bar.foreground : col.dim
            font.family: root.bar.fontFamily
            font.pixelSize: root.status && root.status.stopped ? Style.font.body : Style.font.caption
            wrapMode: Text.WordWrap
          }
          Button {
            text: "Keep private"
            tooltipText: "Restart KDE Connect with its temporary folder in your own runtime folder"
            bordered: true
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.run(["setup-kdeconnect"])
          }
        }

        // The phone: name, link state, battery.
        Column {
          visible: !!root.phone
          width: parent.width
          spacing: Style.space(6)

          Item {
            width: parent.width
            height: Math.max(heroGlyph.implicitHeight, heroText.implicitHeight, heroPercent.implicitHeight)

            Text {
              id: heroGlyph
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: root.glyphs.phone
              color: root.low ? root.bar.urgent : root.bar.foreground
              opacity: root.online ? 1 : 0.5
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title * 1.4
            }

            Column {
              id: heroText
              anchors.left: heroGlyph.right
              anchors.leftMargin: Style.space(10)
              anchors.right: heroPercent.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                width: parent.width
                text: root.phone ? root.phone.name : ""
                textFormat: Text.PlainText
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
                elide: Text.ElideRight
              }
              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: root.phone ? root.linkLine(root.phone) : ""
                color: col.dim
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            Text {
              id: heroPercent
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              visible: root.hasLevel
              textFormat: Text.PlainText
              text: root.hasLevel ? (root.phone.charging ? root.glyphs.charging : "") + root.phone.battery + "%" : ""
              color: root.low ? root.bar.urgent : root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }
          }

          Rectangle {
            visible: root.hasLevel
            width: parent.width
            height: Style.space(4)
            radius: height / 2
            color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.12)

            Rectangle {
              width: root.hasLevel ? parent.width * Math.max(0, Math.min(100, root.phone.battery)) / 100 : 0
              height: parent.height
              radius: parent.radius
              color: root.low ? root.bar.urgent : Color.accent
            }
          }
        }

        // Phones in reach that aren't paired yet, including ones asking to be.
        Repeater {
          model: root.pairable

          delegate: Item {
            id: pairRow
            required property var modelData

            width: col.width
            height: Math.max(pairText.implicitHeight, pairButtons.implicitHeight)

            Column {
              id: pairText
              anchors.left: parent.left
              anchors.right: pairButtons.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                width: parent.width
                text: pairRow.modelData.name
                textFormat: Text.PlainText
                color: pairRow.modelData.pairRequestedByPeer ? root.bar.urgent : root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
                elide: Text.ElideRight
              }
              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: root.pairLine(pairRow.modelData)
                color: col.dim
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            Row {
              id: pairButtons
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(6)

              Button {
                visible: pairRow.modelData.pairRequestedByPeer
                text: "Accept"
                bordered: true
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                fontSize: Style.font.bodySmall
                onClicked: root.run(["accept", pairRow.modelData.id])
              }
              Button {
                visible: pairRow.modelData.pairRequestedByPeer || pairRow.modelData.pairRequested
                text: pairRow.modelData.pairRequestedByPeer ? "Reject" : "Cancel"
                bordered: true
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                fontSize: Style.font.bodySmall
                onClicked: root.run(["reject", pairRow.modelData.id])
              }
              Button {
                visible: !pairRow.modelData.pairRequestedByPeer && !pairRow.modelData.pairRequested
                iconText: root.glyphs.pair
                text: "Pair"
                bordered: true
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                fontSize: Style.font.bodySmall
                iconSize: Style.font.bodySmall
                onClicked: root.run(["pair", pairRow.modelData.id])
              }
            }
          }
        }

        // Ring, clipboard and messages ride KDE Connect; files go over
        // Taildrop; mirror, camera, photos and lock need ADB.
        Flow {
          visible: root.online || root.adbReady || root.canSend
          width: parent.width
          spacing: Style.space(6)

          Button {
            visible: root.online
            iconText: root.glyphs.ring
            text: "Ring"
            tooltipText: "Ring at full volume, even on silent"
            bordered: true
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.ring()
          }
          Button {
            visible: root.adbReady
            iconText: root.glyphs.mirror
            text: root.mirroring ? "Stop mirroring" : "Mirror"
            tooltipText: "Show and control the phone screen in a window (scrcpy)"
            selected: root.mirroring
            bordered: true
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.toggleMirror()
          }
          Button {
            visible: root.canSend
            iconText: root.glyphs.send
            text: "Send files"
            tooltipText: "Pick files to send to the phone. Dropping files on the phone icon in the bar works too."
            bordered: true
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.sendFiles([])
          }
          Button {
            visible: root.adbReady
            iconText: root.glyphs.camera
            text: "Take photo"
            tooltipText: "Open the phone's camera; the photo lands on this PC's clipboard"
            bordered: true
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.launch(["camera"], true)
          }
          Button {
            visible: root.online
            iconText: root.glyphs.clipboard
            text: "Send clipboard"
            tooltipText: "Put this PC's clipboard on the phone"
            bordered: true
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.run(["clipboard", root.phone.id])
          }
          Button {
            visible: root.adbReady
            iconText: root.glyphs.photos
            text: "Import photos"
            tooltipText: "Copy new camera photos to ~/Pictures/Phone (this also happens by itself while the phone is home)"
            bordered: true
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.run(["photos"])
          }
          Button {
            visible: root.online && !!root.status && root.status.sms
            iconText: root.glyphs.messages
            text: "Messages"
            tooltipText: "Read and send the phone's SMS from this PC"
            bordered: true
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.launch(["sms"], true)
          }
          Button {
            visible: root.adbReady
            iconText: root.glyphs.lock
            text: "Lock"
            tooltipText: "Turn the phone's screen off"
            bordered: true
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.run(["lock-phone"])
          }
          Button {
            visible: root.adbOverUsb
            iconText: root.glyphs.pair
            text: "ADB over Wi-Fi"
            tooltipText: "Switch the phone's ADB to Wi-Fi so it works without the cable"
            bordered: true
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.run(["wifi-adb"])
          }
        }

        // Phone-side switches, as a 2-column grid of tiles lit when on.
        Grid {
          visible: root.toggleTiles.length > 0
          width: parent.width
          columns: 2
          spacing: Style.space(6)

          Repeater {
            model: root.toggleTiles

            delegate: Button {
              required property var modelData

              width: (col.width - Style.space(6)) / 2
              leftAlign: true
              iconText: root.glyphs[modelData.key]
              text: modelData.label
              tooltipText: modelData.label + " is " + (modelData.on ? "on" : "off") + " on the phone"
              selected: modelData.on
              bordered: true
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              fontSize: Style.font.bodySmall
              opacity: modelData.on ? 1 : 0.7
              onClicked: root.setToggle(modelData.key, !modelData.on)
            }
          }
        }

        Text {
          visible: root.adbHint !== ""
          width: parent.width
          textFormat: Text.PlainText
          text: root.adbHint
          color: col.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        PanelSectionHeader {
          visible: root.notifications.length > 0
          text: "NOTIFICATIONS"
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
        }

        Repeater {
          model: root.notifications

          delegate: Column {
            id: note
            required property var modelData
            readonly property bool replying: root.replyTarget === note.modelData.id
            // With ADB up, clicking a notification shows its app in the mirror window.
            readonly property bool openable: root.adbReady && !!note.modelData.package

            width: col.width
            spacing: Style.space(6)

            Item {
              width: parent.width
              height: Math.max(noteText.implicitHeight, noteButtons.implicitHeight)

              Column {
                id: noteText
                anchors.left: parent.left
                anchors.right: noteButtons.left
                anchors.rightMargin: Style.space(8)
                anchors.top: parent.top
                spacing: Style.space(2)

                Text {
                  width: parent.width
                  text: note.modelData.title || note.modelData.app
                  textFormat: Text.PlainText
                  color: root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                  elide: Text.ElideRight
                }
                Text {
                  width: parent.width
                  text: (note.modelData.title && note.modelData.app ? note.modelData.app + " · " : "") + note.modelData.text
                  textFormat: Text.PlainText
                  color: col.dim
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                  maximumLineCount: 2
                  elide: Text.ElideRight
                }
              }

              MouseArea {
                anchors.fill: noteText
                enabled: note.openable
                cursorShape: Qt.PointingHandCursor
                onClicked: root.openApp(note.modelData.package)
              }

              Row {
                id: noteButtons
                anchors.right: parent.right
                anchors.top: parent.top
                spacing: Style.space(2)

                PanelActionButton {
                  visible: !!note.modelData.replyId
                  iconText: root.glyphs.reply
                  tooltipText: "Reply"
                  foreground: note.replying ? Color.accent : root.bar.foreground
                  fontFamily: root.bar.fontFamily
                  onClicked: root.startReply(note.modelData.id)
                }
                PanelActionButton {
                  visible: note.modelData.dismissable
                  iconText: root.glyphs.close
                  tooltipText: "Dismiss on the phone"
                  foreground: root.bar.foreground
                  fontFamily: root.bar.fontFamily
                  onClicked: root.run(["dismiss", root.phone.id, note.modelData.id])
                }
              }
            }

            // Inline reply: Enter sends, Escape drops it.
            Rectangle {
              visible: note.replying
              width: parent.width
              height: replyInput.implicitHeight + Style.space(14)
              radius: Style.space(6)
              color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.06)
              border.width: 1
              border.color: Color.accent

              TextInput {
                id: replyInput
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(8)
                anchors.rightMargin: Style.space(8)
                color: root.bar.foreground
                selectionColor: Color.accent
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.bodySmall
                clip: true
                onVisibleChanged: if (visible) { text = ""; forceActiveFocus() }
                Keys.onReturnPressed: root.sendReply(note.modelData.replyId, text)
                Keys.onEnterPressed: root.sendReply(note.modelData.replyId, text)
                Keys.onEscapePressed: root.replyTarget = ""

                Text {
                  anchors.fill: parent
                  visible: replyInput.text.length === 0
                  textFormat: Text.PlainText
                  text: "Reply to " + (note.modelData.title || note.modelData.app) + " · Enter sends"
                  color: col.dim
                  font: replyInput.font
                  elide: Text.ElideRight
                  verticalAlignment: Text.AlignVCenter
                }
              }
            }
          }
        }

        PanelSeparator {
          foreground: root.bar.foreground
        }

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: {
            if (!root.status) return "Reading…"
            var mins = Math.floor((root.nowTick.getTime() / 1000 - root.status.time) / 60)
            return (mins < 1 ? "Updated just now" : "Updated " + mins + " min ago")
              + " · right-click refreshes, middle-click rings, drop files on the icon to send them"
          }
          color: col.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Button {
          visible: !!root.phone
          iconText: root.glyphs.alert
          text: root.lostArmed ? "Click again to cut the phone off" : "Lost phone…"
          tooltipText: "Locks the phone if it's in reach, unpairs it, stops looking for it over Tailscale, drops ADB, and opens the Tailscale admin page so you can remove it"
          bordered: true
          foreground: root.lostArmed ? root.bar.urgent : col.dim
          fontFamily: root.bar.fontFamily
          fontSize: Style.font.bodySmall
          iconSize: Style.font.bodySmall
          onClicked: root.reportLost()
        }
      }
    }
  }
}
