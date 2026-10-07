import QtQuick
import QtQuick.Shapes
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons

// Hot corner. The daemon watches the pointer, so this window
// exists only while the card is up and never covers the rest of the screen.
Item {
  id: root

  property var shell: null
  property var manifest: null

  property bool open: false
  property bool loading: false
  property int session: 0
  property string monitorName: ""
  property string note: ""
  property string errorText: ""
  property string spend: ""
  property string jevInput: ""
  property bool macKeys: false
  property var rows: []
  property bool chomping: false
  property real chompT: 0
  property int cardHeight: 0
  property bool cardHeld: false
  property real placeX: 1
  property real placeY: 1
  property bool moving: false

  readonly property string daemonPath: Qt.resolvedUrl("bin/shortcuts-ai").toString().replace(/^file:\/\//, "")

  function apply(message) {
    // Hide always closes. The daemon advances the session in the same step,
    // so a session check here would drop the message and leave the card up.
    if (message.op === "hide") {
      root.cardHeld = false
      root.open = false
      root.moving = false
      return
    }
    if (message.op === "place") {
      root.takePlace(message)
      if (message.monitor)
        root.monitorName = message.monitor
      return
    }
    if (message.op === "chomp") {
      if (message.monitor)
        root.monitorName = message.monitor
      root.startChomp()
      return
    }
    if (message.session !== undefined && message.op !== "show" && message.session !== root.session)
      return
    if (message.op === "show") {
      root.session = message.session
      root.monitorName = message.monitor || ""
      root.loading = !!message.loading
      root.rows = message.items || []
      root.note = message.note || ""
      root.errorText = message.error || ""
      root.takeDebug(message)
      root.takePlace(message)
      root.open = true
      return
    }
    if (message.op === "update") {
      root.loading = false
      root.rows = message.items || []
      root.note = message.note || ""
      root.errorText = message.error || ""
      root.takeDebug(message)
      root.takePlace(message)
      return
    }
    if (message.op === "ran") {
      if (message.ok) {
        root.open = false
      } else {
        root.errorText = message.note || "That shortcut did not run."
      }
    }
  }

  function takePlace(message) {
    // Moving has to be true before the corner changes. The card jumps out
    // from under the pointer, and that hover-leave must not close it.
    if (message.moving !== undefined)
      root.moving = !!message.moving
    if (message.x !== undefined)
      root.placeX = message.x
    if (message.y !== undefined)
      root.placeY = message.y
  }

  function takeDebug(message) {
    if (message.spend !== undefined)
      root.spend = message.spend || ""
    if (message.input !== undefined)
      root.jevInput = message.input || ""
    if (message.mac !== undefined)
      root.macKeys = !!message.mac
  }

  function run(arg) {
    daemon.write(JSON.stringify({ op: "run", arg: String(arg) }) + "\n")
  }

  function toggleDebug() {
    daemon.write(JSON.stringify({ op: "debug" }) + "\n")
  }

  function leave() {
    // The pointer left the card. Close before the next poll. A click hides
    // the card first, and that must not count as the pointer leaving.
    // An arrow moves the card out from under the pointer. That is not leaving.
    if (!root.open || root.moving)
      return
    root.cardHeld = false
    root.open = false
    daemon.write(JSON.stringify({ op: "away" }) + "\n")
  }

  function startChomp() {
    root.chompT = 0
    root.chomping = true
    chompClock.restart()
  }

  Process {
    id: daemon
    running: true
    stdinEnabled: true
    command: ["python3", root.daemonPath]
    stdout: SplitParser {
      onRead: function(line) {
        try {
          root.apply(JSON.parse(String(line)))
        } catch (e) {}
      }
    }
    stderr: SplitParser {
      onRead: function(line) {
        console.warn("ignotas.shortcuts-ai: " + line)
      }
    }
    onExited: restart.restart()
  }

  Timer {
    id: restart
    interval: 1500
    onTriggered: daemon.running = true
  }

  Timer {
    id: chompClock
    interval: 16
    repeat: true
    onTriggered: {
      root.chompT = Math.min(1, root.chompT + 16 / 800)
      if (root.chompT >= 1) {
        stop()
        root.chomping = false
      }
    }
  }

  Variants {
    model: Quickshell.screens

    PanelWindow {
      id: panel
      required property var modelData
      screen: modelData
      // An empty monitor name still has to draw. The bite can arrive before
      // the card has been shown on this process, and this machine has one screen.
      visible: (root.open || root.chomping) && (root.monitorName === "" || modelData.name === root.monitorName || !modelData.name)
      color: "transparent"
      exclusionMode: ExclusionMode.Ignore
      WlrLayershell.namespace: "ignotas-shortcuts-ai"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
      anchors { top: true; bottom: true; left: true; right: true }
      // Input stays on the card. The bite is drawn on this same window, so it
      // cannot hide behind it, and it does not take the pointer.
      mask: Region { item: root.open ? card : clickless }

      Item {
        id: clickless
        width: 0
        height: 0
      }

      Rectangle {
        id: card
        // Fractions of the free space, so the card stays whole on every spot.
        // Anchors are not used: clearing one left the card stretched.
        x: root.placeX * Math.max(0, parent.width - width)
        y: root.placeY * Math.max(0, parent.height - height)
        readonly property int suggestionWidth: Style.space(440)
        readonly property int inspectorWidth: root.spend !== ""
          ? Math.min(Style.space(620), Math.max(Style.space(320), panel.width - suggestionWidth - Style.space(16)))
          : 0
        width: suggestionWidth + inspectorWidth
        height: Math.max(column.implicitHeight + Style.space(16), root.spend !== "" ? Style.space(300) : 0)
        radius: Style.cornerRadius
        color: Color.popups.background
        border.width: Math.max(1, Style.space(1))
        border.color: Color.popups.border
        onHeightChanged: root.cardHeight = height

        Item {
          id: inspector
          visible: root.spend !== ""
          anchors.left: parent.left
          anchors.top: parent.top
          anchors.bottom: parent.bottom
          width: card.inspectorWidth
          clip: true

          Column {
            id: legend
            visible: root.macKeys
            x: Style.space(10)
            y: Style.space(8)
            width: parent.width - Style.space(20)
            spacing: Style.space(2)

            Row {
              spacing: Style.space(8)
              Text {
                text: "⌘"
                color: Color.accent
                // The UI monospace draws a corner for Option. This face has the Mac logos.
                font.family: "Noto Sans Symbols 2"
                font.pixelSize: Style.font.heading
              }
              Text {
                text: "Command"
                anchors.verticalCenter: parent.verticalCenter
                color: Color.popups.text
                font.family: Style.font.family
                font.pixelSize: Style.font.body
              }
              Text {
                text: "Super"
                anchors.verticalCenter: parent.verticalCenter
                color: Color.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }
            Row {
              spacing: Style.space(8)
              Text {
                text: "⌥"
                color: Color.accent
                font.family: "Noto Sans Symbols 2"
                font.pixelSize: Style.font.heading
              }
              Text {
                text: "Option"
                anchors.verticalCenter: parent.verticalCenter
                color: Color.popups.text
                font.family: Style.font.family
                font.pixelSize: Style.font.body
              }
              Text {
                text: "Alt"
                anchors.verticalCenter: parent.verticalCenter
                color: Color.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }
          }

          Text {
            id: inputLabel
            x: Style.space(10)
            width: parent.width - debugHint.width - Style.space(24)
            anchors.top: legend.visible ? legend.bottom : parent.top
            anchors.topMargin: Style.space(8)
            text: "Last input"
            color: Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }

          Item {
            id: debugHint
            anchors.right: parent.right
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: inputLabel.verticalCenter
            width: debugHintRow.implicitWidth + Style.space(8)
            height: Math.max(debugHintRow.implicitHeight, Style.space(22))

            Row {
              id: debugHintRow
              anchors.centerIn: parent
              spacing: Style.space(4)
              Text {
                text: "D"
                color: Color.accent
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                font.bold: true
              }
              Text {
                text: "toggles debug"
                anchors.verticalCenter: parent.verticalCenter
                color: Color.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }

            MouseArea {
              anchors.fill: parent
              onClicked: root.toggleDebug()
            }
          }

          Flickable {
            id: inputScroll
            x: Style.space(10)
            width: parent.width - Style.space(20)
            anchors.top: inputLabel.bottom
            anchors.topMargin: Style.space(4)
            anchors.bottom: parent.bottom
            anchors.bottomMargin: Style.space(8)
            clip: true
            contentWidth: width
            contentHeight: inputText.implicitHeight
            flickableDirection: Flickable.VerticalFlick
            boundsBehavior: Flickable.StopAtBounds

            Text {
              id: inputText
              width: inputScroll.width
              wrapMode: Text.Wrap
              text: root.jevInput !== "" ? root.jevInput : "No input yet."
              color: Color.popups.text
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            // Above the text: a wheel on the glyphs never reaches a sibling behind them.
            // A trackpad step is one pixel. A mouse notch is 72px. The request is long.
            MouseArea {
              anchors.fill: parent
              acceptedButtons: Qt.NoButton
              onWheel: function(wheel) {
                var dy = wheel.pixelDelta.y
                if (dy === 0)
                  dy = wheel.angleDelta.y / 120 * 72
                dy *= 5
                var limit = Math.max(0, inputScroll.contentHeight - inputScroll.height)
                var next = inputScroll.contentY - dy
                if (next < 0)
                  next = 0
                else if (next > limit)
                  next = limit
                inputScroll.contentY = next
                wheel.accepted = true
              }
            }
          }
        }

        Rectangle {
          visible: inspector.visible
          width: Math.max(1, Style.space(1))
          anchors.left: inspector.right
          anchors.top: parent.top
          anchors.bottom: parent.bottom
          color: Color.popups.border
        }

        Column {
          id: column
          anchors.left: inspector.right
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          anchors.leftMargin: Style.space(8)
          anchors.rightMargin: Style.space(8)
          anchors.bottomMargin: Style.space(8)
          spacing: Style.space(2)

          Text {
            width: column.width
            leftPadding: Style.space(8)
            text: root.loading ? "Looking…" : (root.errorText || root.note || "Try")
            color: root.errorText ? Color.urgent : Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }

          Repeater {
            model: root.rows
            delegate: Rectangle {
              required property var modelData
              required property int index
              width: column.width
              height: Style.space(36)
              radius: Style.space(6)
              color: hover.containsPress ? Color.menu.selectedBackground : "transparent"

              Text {
                id: chordText
                x: Style.space(8)
                width: Style.space(220)
                anchors.verticalCenter: parent.verticalCenter
                text: modelData.chord
                color: Color.accent
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideRight
              }
              Text {
                x: chordText.x + chordText.width + Style.space(8)
                width: parent.width - x - Style.space(44)
                anchors.verticalCenter: parent.verticalCenter
                text: modelData.description
                color: Color.popups.text
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
              }
              Text {
                anchors.right: parent.right
                anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                text: (modelData.percent === "" || modelData.percent === undefined) ? "" : modelData.percent + "%"
                visible: text !== ""
                color: Color.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }

              MouseArea {
                id: hover
                anchors.fill: parent
                onClicked: root.run(modelData.arg)
              }
            }
          }

          Text {
            width: column.width
            leftPadding: Style.space(8)
            visible: root.spend !== ""
            text: root.spend
            color: Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }

        // Tracks the card only. It does not take clicks, so the rows still do.
        HoverHandler {
          onHoveredChanged: {
            if (hovered) {
              root.cardHeld = true
              return
            }
            if (root.open && root.cardHeld)
              root.leave()
          }
        }
      }

      // Same surface as the card, in the corner the pointer is already in.
      // A second window mapped under the cursor and the card's leave handler
      // cancelled the bite before a frame showed.
      Item {
        id: bite
        visible: root.chomping
        enabled: false
        z: 2
        width: 146
        height: 56
        x: card.x + (root.placeX >= 0.5 ? Math.max(0, card.width - width - 8) : 8)
        y: card.y + (root.placeY >= 0.5 ? Math.max(0, card.height - height - 6) : 6)

        Repeater {
          model: 4
          Rectangle {
            required property int index
            width: 5
            height: 5
            radius: 3
            color: "#b4b4c0"
            anchors.verticalCenter: parent.verticalCenter
            x: 36 + index * 16
            // Gone once Pac-Man's mouth reaches the pellet.
            visible: pac.x + 18 < x
          }
        }

        Item {
          id: letter
          width: 42
          height: 44
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          opacity: root.chompT < 0.5 ? 1 : Math.max(0, 1 - (root.chompT - 0.5) / 0.28)

          // Straight stem, round bowl: the silhouette is a D. The black
          // counter is the hole of that D, with the ghost eyes inside it.
          Shape {
            anchors.fill: parent
            preferredRendererType: Shape.CurveRenderer

            ShapePath {
              fillColor: "#f4f1ff"
              strokeColor: "#2c2840"
              strokeWidth: 1.4
              joinStyle: ShapePath.RoundJoin
              startX: 2
              startY: 28
              PathLine { x: 2; y: 3 }
              PathLine { x: 16; y: 3 }
              PathAngleArc {
                centerX: 16
                centerY: 15.5
                radiusX: 14
                radiusY: 12.5
                startAngle: -90
                sweepAngle: 180
              }
              PathLine { x: 2; y: 28 }
            }
          }

          Row {
            x: 2
            y: 24
            spacing: 0
            Repeater {
              model: 3
              Rectangle {
                required property int index
                width: 10
                height: 12
                radius: 5
                color: "#f4f1ff"
                border.color: "#2c2840"
                border.width: 1
              }
            }
          }

          Rectangle {
            x: 3
            y: 24
            width: 26
            height: 8
            color: "#f4f1ff"
          }

          Shape {
            anchors.fill: parent
            preferredRendererType: Shape.CurveRenderer

            ShapePath {
              fillColor: "#14121c"
              strokeColor: "#14121c"
              strokeWidth: 1
              joinStyle: ShapePath.RoundJoin
              startX: 8
              startY: 24
              PathLine { x: 8; y: 7 }
              PathLine { x: 15; y: 7 }
              PathAngleArc {
                centerX: 15
                centerY: 15.5
                radiusX: 9
                radiusY: 8.5
                startAngle: -90
                sweepAngle: 180
              }
              PathLine { x: 8; y: 24 }
            }
          }

          Row {
            x: 12
            y: 11
            spacing: 2
            Repeater {
              model: 2
              Item {
                required property int index
                width: 6
                height: 7
                Rectangle {
                  anchors.fill: parent
                  radius: 3
                  color: "#ffffff"
                }
                Rectangle {
                  width: 3
                  height: 3
                  radius: 1.5
                  color: "#1a1408"
                  x: 0.5
                  y: 2.6
                }
              }
            }
          }
        }

        Item {
          id: pac
          width: 28
          height: 28
          anchors.verticalCenter: parent.verticalCenter
          readonly property real tick: root.chompT
          readonly property real mouth: 10 + Math.abs(Math.sin(tick * Math.PI * 3)) * 46
          x: {
            var travel = Math.max(0, letter.x - width + 8)
            var u = Math.min(1, tick / 0.45)
            u = u * u * (3 - 2 * u)
            return u * travel
          }

          Shape {
            anchors.fill: parent
            preferredRendererType: Shape.CurveRenderer

            ShapePath {
              fillColor: "#ffcc00"
              strokeColor: "#1a1408"
              strokeWidth: 1.5
              startX: 14
              startY: 14
              PathAngleArc {
                centerX: 14
                centerY: 14
                radiusX: 12
                radiusY: 12
                startAngle: pac.mouth
                sweepAngle: 360 - pac.mouth * 2
              }
              PathLine { x: 14; y: 14 }
            }
          }

          Rectangle {
            width: 4
            height: 4
            radius: 2
            color: "#1a1408"
            x: 12
            y: 6
          }
        }
      }
    }
  }
}
