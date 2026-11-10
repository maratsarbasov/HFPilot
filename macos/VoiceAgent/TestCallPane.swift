import Cocoa
import AVFoundation

private final class TestInstructionsEditor: NSTextView {
    var placeholder = "" { didSet { needsDisplay = true } }
    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty else { return }
        let rect = bounds.insetBy(dx: textContainerInset.width + 5, dy: textContainerInset.height)
        (placeholder as NSString).draw(in: rect, withAttributes: [
            .font: font ?? NSFont.systemFont(ofSize: 14), .foregroundColor: NSColor.placeholderTextColor])
    }
}

final class TestCallPane: NSView, NSTextViewDelegate {
    private enum Phase { case idle, permission, starting, connecting, connected, stopping, summarizing, finished }
    private let bridge: DesktopBridge
    private let incomingInstructions: () -> String
    private let showConversations: (String) -> Void
    private let showSettings: () -> Void
    private let direction = NSSegmentedControl(labels: ["Incoming", "Outgoing"], trackingMode: .selectOne, target: nil, action: nil)
    private let explanation = NSTextField(wrappingLabelWithString: "")
    private let editor = TestInstructionsEditor()
    private let primary = NSButton(title: "Start Test", target: nil, action: nil)
    private let useIncoming = NSButton(title: "Use Incoming Instructions", target: nil, action: nil)
    private let result = NSButton(title: "View Result", target: nil, action: nil)
    private let remedy = NSButton(title: "Open Settings", target: nil, action: nil)
    private let statusTitle = NSTextField(labelWithString: "")
    private let statusDetail = NSTextField(wrappingLabelWithString: "")
    private let recordingNote = NSTextField(labelWithString: "")
    private let progress = NSProgressIndicator()
    private let activeView = NSView()
    private let activeTitle = NSTextField(labelWithString: "")
    private let elapsed = NSTextField(labelWithString: "00:00")
    private var form: NSStackView!
    private var phase = Phase.idle
    private var drafts = ["", ""]
    private var incomingEdited = false
    private var selectedDirection = 0
    private var configured = false
    private var activeRuntimeID: String?
    private var operation: String?
    private var callID: String?
    private var completedID: String?
    private var resultID: String?
    private var connectedAt: Date?
    private var error: String?
    private var pollingID: String?
    private var timer: Timer?
    private var cancelRequested = false

    private var locked: Bool { [.permission, .starting, .connecting, .connected, .stopping].contains(phase) }
    private var otherCall: Bool { activeRuntimeID != nil && activeRuntimeID != callID && activeRuntimeID != completedID }
    private var instructionCount: Int { editor.string.trimmingCharacters(in: .whitespacesAndNewlines).unicodeScalars.count }
    private var microphoneBlocked: Bool { [.denied, .restricted].contains(AVCaptureDevice.authorizationStatus(for: .audio)) }

    init(bridge: DesktopBridge, incomingInstructions: @escaping () -> String,
         showConversations: @escaping (String) -> Void, showSettings: @escaping () -> Void) {
        self.bridge = bridge
        self.incomingInstructions = incomingInstructions
        self.showConversations = showConversations
        self.showSettings = showSettings
        super.init(frame: .zero)
        let title = NSTextField(labelWithString: "Rehearse a call")
        title.font = .systemFont(ofSize: 21, weight: .semibold)
        let intro = caption("Talk to the assistant on this Mac before using your phone.")
        direction.selectedSegment = 0
        direction.target = self
        direction.action = #selector(directionChanged)
        explanation.font = .systemFont(ofSize: 13)
        explanation.textColor = .secondaryLabelColor
        let instructionTitle = NSTextField(labelWithString: "Assistant’s instructions")
        instructionTitle.font = .systemFont(ofSize: 13, weight: .semibold)
        let spacer = NSView()
        let instructionHeader = NSStackView(views: [instructionTitle, spacer, useIncoming])
        instructionHeader.spacing = 12
        editor.isRichText = false
        editor.font = .systemFont(ofSize: 14)
        editor.textColor = .labelColor
        editor.drawsBackground = false
        editor.textContainerInset = NSSize(width: 10, height: 12)
        editor.isAutomaticQuoteSubstitutionEnabled = false
        editor.isAutomaticDashSubstitutionEnabled = false
        editor.allowsUndo = true
        editor.isVerticallyResizable = true
        editor.isHorizontallyResizable = false
        editor.autoresizingMask = [.width]
        editor.textContainer?.widthTracksTextView = true
        editor.delegate = self
        editor.setAccessibilityLabel("Test call instructions")
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        scroll.documentView = editor
        let box = NSBox()
        box.boxType = .custom
        box.titlePosition = .noTitle
        box.cornerRadius = 9
        box.borderColor = .separatorColor
        box.borderWidth = 1
        box.fillColor = .textBackgroundColor
        box.contentViewMargins = NSSize(width: 1, height: 1)
        box.contentView = scroll
        form = column([direction, explanation, instructionHeader, box, caption("Edits apply only to this test.")], spacing: 8)
        instructionHeader.widthAnchor.constraint(equalTo: form.widthAnchor).isActive = true
        box.widthAnchor.constraint(equalTo: form.widthAnchor).isActive = true
        box.heightAnchor.constraint(greaterThanOrEqualToConstant: 80).isActive = true
        explanation.widthAnchor.constraint(equalTo: form.widthAnchor).isActive = true
        buildActiveView()
        let devices = caption("Default microphone → Assistant → Default output")
        recordingNote.font = .systemFont(ofSize: 11)
        recordingNote.textColor = .secondaryLabelColor
        let audio = column([devices, recordingNote], spacing: 4)
        let statusText = column([statusTitle, statusDetail], spacing: 3)
        statusTitle.font = .systemFont(ofSize: 13, weight: .semibold)
        statusDetail.font = .systemFont(ofSize: 12)
        statusDetail.textColor = .secondaryLabelColor
        statusDetail.widthAnchor.constraint(equalTo: statusText.widthAnchor).isActive = true
        progress.style = .spinning
        progress.controlSize = .small
        progress.isDisplayedWhenStopped = false
        let statusRow = NSStackView(views: [progress, statusText, primary])
        statusRow.alignment = .centerY
        statusRow.distribution = .fill
        statusRow.spacing = 12
        statusText.setHuggingPriority(.defaultLow, for: .horizontal)
        statusText.setContentHuggingPriority(.defaultLow, for: .horizontal)
        statusText.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        primary.setContentCompressionResistancePriority(.required, for: .horizontal)
        primary.widthAnchor.constraint(equalToConstant: 112).isActive = true
        let links = NSStackView(views: [result, remedy])
        links.spacing = 12
        let footer = column([statusRow, links], spacing: 8)
        statusRow.widthAnchor.constraint(equalTo: footer.widthAnchor).isActive = true
        for (button, action) in [(primary, #selector(primaryAction)), (useIncoming, #selector(copyIncoming)),
                                  (result, #selector(viewResult)), (remedy, #selector(openRemedy))] {
            button.target = self
            button.action = action
            button.bezelStyle = .rounded
        }
        useIncoming.controlSize = .small
        result.controlSize = .small
        remedy.controlSize = .small
        let line = NSBox(); line.boxType = .separator
        let stack = column([title, intro, form, activeView, audio, line, footer], spacing: 10)
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 20),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -20)
        ])
        for view in [intro, form!, activeView, audio, line, footer] {
            view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        directionChanged()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }
    deinit { timer?.invalidate() }

    private func column(_ views: [NSView], spacing: CGFloat) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = spacing
        return stack
    }
    private func caption(_ text: String) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: 12)
        label.textColor = .secondaryLabelColor
        return label
    }
    private func buildActiveView() {
        let icon = NSImageView(image: NSImage(systemSymbolName: "waveform", accessibilityDescription: nil)!)
        icon.contentTintColor = .controlAccentColor
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.widthAnchor.constraint(equalToConstant: 48).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 48).isActive = true
        activeTitle.font = .systemFont(ofSize: 19, weight: .semibold)
        elapsed.font = .monospacedDigitSystemFont(ofSize: 28, weight: .regular)
        let note = caption("You can end this test at any time.")
        let content = column([icon, activeTitle, elapsed, note], spacing: 14)
        content.alignment = .centerX
        content.translatesAutoresizingMaskIntoConstraints = false
        activeView.addSubview(content)
        NSLayoutConstraint.activate([
            content.centerXAnchor.constraint(equalTo: activeView.centerXAnchor),
            content.centerYAnchor.constraint(equalTo: activeView.centerYAnchor),
            activeView.heightAnchor.constraint(greaterThanOrEqualToConstant: 160)
        ])
    }

    func update(configured: Bool, runtime: [String: Any], operation: String?, recording: [String: Any]) {
        self.configured = configured
        self.activeRuntimeID = runtime["active_call_id"] as? String
        self.operation = operation
        let preference = runtime["recording"] as? [String: Any] ?? recording
        let recordingState = (preference["enabled"] as? Bool).map { $0 ? "Audio recording on" : "Audio recording off" } ?? "Check recording in Settings"
        recordingNote.stringValue = recordingState + " · Model API usage applies"
        if !incomingEdited {
            drafts[0] = incomingInstructions()
            if selectedDirection == 0 && !locked { setEditor(drafts[0]) }
        }
        if callID == nil && !locked, runtime["active_test_call"] as? Bool == true,
           let id = activeRuntimeID, id != completedID {
            callID = id
            phase = .connecting
            watch()
        }
        render()
    }

    private func setEditor(_ text: String) {
        if editor.string != text { editor.string = text; editor.needsDisplay = true }
    }
    @objc private func directionChanged() {
        drafts[selectedDirection] = editor.string
        selectedDirection = direction.selectedSegment
        setEditor(drafts[selectedDirection])
        editor.undoManager?.removeAllActions()
        explanation.stringValue = selectedDirection == 0
            ? "You play the caller. The assistant answers your call."
            : "You play the recipient. The assistant explains why it is calling."
        editor.placeholder = selectedDirection == 0 ? "Describe how the assistant should answer your call."
            : "Example: Call a repair shop to ask whether order #42 is ready. Ask about pickup hours. Do not agree to extra charges."
        error = nil
        render()
    }
    @objc private func copyIncoming() {
        editor.insertText(incomingInstructions(), replacementRange: NSRange(location: 0, length: (editor.string as NSString).length))
        textDidChange(Notification(name: NSText.didChangeNotification))
    }
    func textDidChange(_ notification: Notification) {
        drafts[selectedDirection] = editor.string
        if selectedDirection == 0 { incomingEdited = true }
        editor.needsDisplay = true
        error = nil
        render()
    }

    private func render() {
        form.isHidden = locked
        activeView.isHidden = !locked
        useIncoming.isHidden = selectedDirection != 0
        editor.isEditable = !locked
        direction.isEnabled = !locked
        let working = [.permission, .starting, .connecting, .stopping, .summarizing].contains(phase)
        progress.isHidden = !working
        if working { progress.startAnimation(nil) } else { progress.stopAnimation(nil) }
        primary.title = locked ? (phase == .connected ? "End Test" : "Cancel Test") : "Start Test"
        primary.bezelColor = locked ? .systemRed : .controlAccentColor
        primary.contentTintColor = .white
        primary.isEnabled = locked ? phase != .stopping && !cancelRequested :
            configured && !microphoneBlocked && !otherCall && operation == nil && (1...32000).contains(instructionCount)
        result.isHidden = resultID == nil
        remedy.isHidden = locked || (configured && !microphoneBlocked)
        remedy.title = configured ? "Microphone Settings…" : "Open Settings…"
        statusTitle.textColor = .labelColor
        activeTitle.stringValue = selectedDirection == 0 ? "Incoming test" : "Outgoing test"
        let seconds = connectedAt.map { max(0, Int(Date().timeIntervalSince($0))) } ?? 0
        elapsed.stringValue = String(format: "%02d:%02d", seconds / 60, seconds % 60)
        elapsed.isHidden = connectedAt == nil
        switch phase {
        case .permission: statusTitle.stringValue = "Microphone access"; statusDetail.stringValue = "Allow access in the macOS dialog."
        case .starting, .connecting: statusTitle.stringValue = "Connecting…"; statusDetail.stringValue = "Wait for the assistant’s greeting."
        case .connected: statusTitle.stringValue = "Connected · Speak now"; statusDetail.stringValue = "You are talking to the assistant through your Mac."
        case .stopping: statusTitle.stringValue = "Ending test…"; statusDetail.stringValue = "Releasing the microphone and audio."
        case .summarizing: statusTitle.stringValue = "Test ended"; statusDetail.stringValue = "Preparing the report. You can read the transcript now."
        case .finished: statusTitle.stringValue = "Test finished"; statusDetail.stringValue = "Review the result or adjust the instructions and try again."
        case .idle: statusTitle.stringValue = "Ready to test"; statusDetail.stringValue = "No phone is needed and no number will be dialed."
        }
        if !locked {
            if !configured { statusTitle.stringValue = "API key needed"; statusDetail.stringValue = "Add your OpenAI API key in Settings." }
            else if microphoneBlocked { statusTitle.stringValue = "Microphone access needed"; statusDetail.stringValue = "Allow HFPilot in macOS Microphone settings." }
            else if otherCall { statusTitle.stringValue = "Another call is in progress"; statusDetail.stringValue = "Finish it before starting this test." }
            else if operation != nil { statusTitle.stringValue = "Please wait"; statusDetail.stringValue = "An application operation is still in progress." }
            else if instructionCount == 0 { statusTitle.stringValue = "Add instructions to begin"; statusDetail.stringValue = "Describe the assistant’s task, role and language." }
            else if instructionCount > 32000 { statusTitle.stringValue = "Instructions are too long"; statusDetail.stringValue = "Use 32,000 characters or fewer." }
        }
        if let error {
            statusTitle.stringValue = locked ? "Connection needs attention" : "Test needs attention"
            statusTitle.textColor = .systemRed
            statusDetail.stringValue = error
        }
    }

    @objc private func primaryAction() { if locked { end() } else { begin() } }
    private func begin() {
        guard primary.isEnabled, !locked else { return }
        timer?.invalidate()
        callID = nil
        resultID = nil
        connectedAt = nil
        error = nil
        cancelRequested = false
        phase = .permission
        render()
        let permitted: (Bool) -> Void = { [weak self] allowed in
            DispatchQueue.main.async {
                guard let self = self, self.phase == .permission else { return }
                guard allowed else { self.phase = .idle; self.render(); return }
                self.phase = .starting
                self.render()
                self.bridge.data("test_call", values: ["command": "start",
                    "direction": self.selectedDirection == 0 ? "incoming" : "outgoing", "assignment": self.editor.string]) { [weak self] response in
                    guard let self = self else { return }
                    if response.code == 0, let id = response.object["call_id"] as? String {
                        self.callID = id
                        self.phase = self.cancelRequested ? .stopping : .connecting
                        if self.cancelRequested { self.sendStop() }
                        self.watch()
                    } else {
                        self.phase = .idle
                        self.error = response.error
                        self.cancelRequested = false
                    }
                    self.render()
                }
            }
        }
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            AVCaptureDevice.requestAccess(for: .audio, completionHandler: permitted)
        } else { permitted(AVCaptureDevice.authorizationStatus(for: .audio) == .authorized) }
    }
    private func watch() {
        timer?.invalidate()
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in self?.render(); self?.poll() }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
        poll()
    }
    private func poll() {
        guard let id = callID, pollingID != id else { return }
        pollingID = id
        bridge.data("test_call", values: ["command": "status", "call_id": id]) { [weak self] response in
            guard let self = self else { return }
            if self.pollingID == id { self.pollingID = nil }
            guard self.callID == id else { return }
            guard response.code == 0 else {
                self.error = "Could not read test status. Retrying automatically. \(response.error)"
                self.render()
                return
            }
            self.error = nil
            let job = response.object
            self.resultID = job["conversation_id"] as? String
            if self.connectedAt == nil, let value = job["connected_at"] as? String {
                let formatter = ISO8601DateFormatter()
                formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
                self.connectedAt = formatter.date(from: value)
            }
            // Restore the actual scenario if the UI reattaches to a running test.
            if self.locked, let assignment = job["assignment"] as? String {
                self.selectedDirection = job["direction"] as? String == "outgoing" ? 1 : 0
                self.direction.selectedSegment = self.selectedDirection
                self.setEditor(assignment)
                self.drafts[self.selectedDirection] = assignment
                self.incomingEdited = self.incomingEdited || self.selectedDirection == 0
                self.explanation.stringValue = self.selectedDirection == 0
                    ? "You play the caller. The assistant answers your call."
                    : "You play the recipient. The assistant explains why it is calling."
            }
            switch job["status"] as? String {
            case "completed":
                let saved = job["result"] as? [String: Any] ?? [:]
                self.error = saved["error"] as? String
                self.phase = .finished
                self.completedID = id
                self.callID = nil
                self.cancelRequested = false
                self.timer?.invalidate()
                self.timer = nil
            case "summarizing":
                self.phase = .summarizing
                self.cancelRequested = false
            default:
                self.phase = self.cancelRequested ? .stopping : (job["phase"] as? String == "connected" ? .connected : .connecting)
            }
            self.render()
        }
    }
    private func end() {
        if phase == .permission { phase = .idle; render(); return }
        guard !cancelRequested else { return }
        cancelRequested = true
        if callID != nil { sendStop() }
        phase = .stopping
        render()
    }
    private func sendStop() {
        guard let id = callID else { return }
        bridge.data("test_call", values: ["command": "stop", "call_id": id]) { [weak self] response in
            guard let self = self, self.callID == id else { return }
            if response.code != 0 {
                self.cancelRequested = false
                self.phase = self.connectedAt == nil ? .connecting : .connected
                self.error = response.error
                self.render()
            }
        }
    }
    @objc private func viewResult() { if let id = resultID { showConversations(id) } }
    @objc private func openRemedy() {
        if !configured { showSettings() }
        else { NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!) }
    }
}
