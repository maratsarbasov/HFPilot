import Cocoa

// Setup owns only its child process. Closing the window cancels and waits for
// Bluetooth cleanup; the Python controller restores the prior assistant state.
final class PhoneSetupWindow: NSWindowController, NSWindowDelegate {
    private let root: URL
    private let runningChanged: (Bool) -> Void
    private let assistantEnabled: Bool
    private let turnOn: (() -> Void)?
    private var process: Process?
    private var input: FileHandle?
    private var confirmationID: Int?
    private var phoneName = "your phone"
    private var comparisonCode: String?
    private var outcome: String?
    private var closing = false
    private let heading = NSTextField(labelWithString: "Connect your phone")
    private let instructions = NSTextField(wrappingLabelWithString: "On your phone, open Settings > Bluetooth and select HFPilot.")
    private let status = NSTextField(wrappingLabelWithString: "Preparing Bluetooth…")
    private let code = NSTextField(labelWithString: "")
    private let spinner = NSProgressIndicator()
    private let primary = NSButton(title: "Confirm", target: nil, action: nil)
    private let secondary = NSButton(title: "Cancel", target: nil, action: nil)
    var isRunning: Bool { process != nil }

    init(root: URL, assistantEnabled: Bool = false, turnOn: (() -> Void)? = nil, runningChanged: @escaping (Bool) -> Void) {
        self.root = root
        self.assistantEnabled = assistantEnabled
        self.turnOn = turnOn
        self.runningChanged = runningChanged
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 460, height: 280),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Set Up Phone"
        window.isReleasedWhenClosed = false
        super.init(window: window)
        window.delegate = self
        heading.font = .boldSystemFont(ofSize: NSFont.systemFontSize + 2)
        code.font = .monospacedDigitSystemFont(ofSize: 26, weight: .medium)
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        primary.bezelStyle = .rounded; secondary.bezelStyle = .rounded
        primary.target = self; primary.action = #selector(primaryClicked)
        primary.keyEquivalent = "\r"
        secondary.target = self; secondary.action = #selector(cancel)
        secondary.keyEquivalent = "\u{1b}"
        let activity = NSStackView(views: [spinner, status])
        activity.orientation = .horizontal; activity.spacing = 8; activity.alignment = .centerY
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let buttons = NSStackView(views: [spacer, secondary, primary])
        buttons.orientation = .horizontal; buttons.spacing = 8
        let stack = NSStackView(views: [heading, instructions, activity, code, buttons])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 24),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: window.contentView!.bottomAnchor, constant: -20),
            instructions.widthAnchor.constraint(equalTo: stack.widthAnchor),
            buttons.widthAnchor.constraint(equalTo: stack.widthAnchor),
            activity.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
        window.center()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    func present() {
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        if process == nil { start() }
    }

    private func start() {
        outcome = nil; closing = false; confirmationID = nil; comparisonCode = nil
        phoneName = "your phone"
        heading.stringValue = "Connect your phone"
        instructions.stringValue = "On your phone, open Settings > Bluetooth and select HFPilot."
        status.stringValue = "Preparing Bluetooth…"
        code.stringValue = ""; primary.isHidden = true
        secondary.title = "Cancel"; secondary.isEnabled = true; secondary.isHidden = false
        spinner.startAnimation(nil)
        let process = Process()
        process.executableURL = AppPaths.backend
        process.arguments = ["bench", "pair"]
        process.currentDirectoryURL = root
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        environment["HFPILOT_DATA_ROOT"] = root.path
        environment.removeValue(forKey: "PYTHONHOME")
        environment.removeValue(forKey: "PYTHONPATH")
        process.environment = environment
        let output = Pipe(), commands = Pipe()
        process.standardOutput = output; process.standardInput = commands
        // Comparison codes exist only in the private stdout pipe, never in a log.
        let logURL = root.appendingPathComponent(".work/mcp/pairing.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
        let log = try? FileHandle(forWritingTo: logURL)
        process.standardError = log ?? FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            try? log?.close()
            status.stringValue = "Phone setup could not start. Check the application log."
            outcome = "error"; stopped()
            return
        }
        self.process = process
        input = commands.fileHandleForWriting
        runningChanged(true)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var pending = Data()
            while true {
                let data = output.fileHandleForReading.availableData
                if data.isEmpty { break }
                pending.append(data)
                while let newline = pending.firstIndex(of: 10) {
                    let line = pending.prefix(upTo: newline)
                    pending.removeSubrange(...newline)
                    if let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                        RunLoop.main.perform(inModes: [.default, .eventTracking]) { self?.received(event) }
                    }
                }
                if pending.count > 65536 { pending.removeAll() }
            }
            process.waitUntilExit()
            try? log?.close()
            RunLoop.main.perform(inModes: [.default, .eventTracking]) { self?.stopped() }
        }
    }

    private func received(_ event: [String: Any]) {
        guard !closing else { return }
        switch event["event"] as? String {
        case "preparing": status.stringValue = "Preparing Bluetooth…"
        case "waiting": status.stringValue = "Waiting for your phone…"
        case "device":
            status.stringValue = "Connecting to your phone…"
        case "securing":
            status.stringValue = "Setting up a secure connection…"
        case "name":
            phoneName = event["name"] as? String ?? "your phone"
            if confirmationID != nil { showConfirmation() }
        case "confirm":
            confirmationID = event["id"] as? Int
            comparisonCode = event["code"] as? String
            showConfirmation()
        case "connecting":
            confirmationID = nil; comparisonCode = nil
            primary.isHidden = true; code.stringValue = ""
            status.stringValue = "Connecting to \(phoneName)…"
            instructions.stringValue = "Complete any pairing request on your phone."
            spinner.startAnimation(nil)
        case "complete":
            outcome = "complete"
            status.stringValue = "\(event["name"] as? String ?? "Phone") added"
            instructions.stringValue = assistantEnabled ? "Your phone is saved. HFPilot is reconnecting to resume answering calls." : "Your phone is saved. Turn on HFPilot to start answering calls."
            code.stringValue = ""
        case "cancelled": outcome = "cancelled"
        case "error":
            outcome = "error"
            status.stringValue = event["message"] as? String ?? "Could not connect. Please try again."
            instructions.stringValue = "You can try again from this window."
            code.stringValue = ""
        default: break
        }
    }

    private func showConfirmation() {
        spinner.stopAnimation(nil)
        heading.stringValue = "Connect to \(phoneName)?"
        if let comparisonCode = comparisonCode {
            instructions.stringValue = "Check that this code matches the one on your phone, then confirm on both devices."
            code.stringValue = comparisonCode
        } else {
            instructions.stringValue = "Confirm that this is the phone you want to use with HFPilot."
            code.stringValue = ""
        }
        status.stringValue = "Waiting for confirmation"
        primary.title = "Confirm"; primary.isHidden = false; primary.isEnabled = true
    }

    private func send(_ value: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: value) else { return }
        try? input?.write(contentsOf: data + Data([10]))
    }
    @objc private func primaryClicked() {
        if process == nil && outcome == "complete" {
            window?.close()
            if !assistantEnabled { turnOn?() }
            return
        }
        if process == nil { start(); return }
        guard let id = confirmationID else { return }
        primary.isEnabled = false
        send(["command": "confirm", "id": id])
    }
    @objc func cancel() {
        guard process != nil else { window?.close(); return }
        closing = true
        confirmationID = nil; comparisonCode = nil; code.stringValue = ""
        primary.isHidden = true; secondary.isEnabled = false
        status.stringValue = "Closing Bluetooth setup…"
        spinner.startAnimation(nil)
        send(["command": "cancel"])
        try? input?.close(); input = nil
    }
    private func stopped() {
        let wasRunning = process != nil
        process = nil
        try? input?.close(); input = nil
        confirmationID = nil; comparisonCode = nil; code.stringValue = ""
        spinner.stopAnimation(nil)
        if wasRunning { runningChanged(false) }
        if closing || outcome == "cancelled" { window?.close(); return }
        secondary.isEnabled = true; secondary.title = "Done"
        if outcome == "complete" {
            primary.title = assistantEnabled ? "Done" : "Turn On"
            primary.isHidden = false
            primary.isEnabled = true
            secondary.isHidden = assistantEnabled
        } else {
            if outcome == nil { status.stringValue = "Phone setup stopped unexpectedly. Check the application log and try again." }
            primary.title = "Try Again"; primary.isHidden = false; primary.isEnabled = true
        }
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if process != nil { cancel(); return false }
        return true
    }
}
