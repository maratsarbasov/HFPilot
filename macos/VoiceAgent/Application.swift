import Cocoa
import CoreBluetooth
import Darwin

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, CBCentralManagerDelegate {
    private var root = URL(fileURLWithPath: "/")
    private var bridge: DesktopBridge!
    private var tunnel: TunnelController!
    private var lockFD: Int32 = -1
    private var statusItem: NSStatusItem!
    private let menu = NSMenu()
    private let statusHeader = StatusHeader()
    private let stateLine = NSMenuItem(title: "Checking status…", action: nil, keyEquivalent: "")
    private let phoneLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private var toggleItem: NSMenuItem!
    private var endItem: NSMenuItem!
    private var library: LibraryWindow?
    private var phoneSetup: PhoneSetupWindow?
    private var timer: Timer?
    private var refreshing = false
    private var known = false
    private var serverRunning = false
    private var runtime: [String: Any] = [:]
    private var operation: String?
    private var lastError: String?
    private var epoch = 0
    private var setupData: [String: Any] = [:]
    private var permissionManager: CBCentralManager?
    private var permissionAction = "enable"
    private var quitRequested = false
    private var quitAfterCall = false
    private var lastConversationState = ""
    private let session = URLSession(configuration: .ephemeral)

    private var enabled: Bool { runtime["enabled"] as? Bool ?? serverRunning }
    private var active: Bool { runtime["active_call_id"] as? String != nil }
    private var pending: Int { runtime["pending_summaries"] as? Int ?? 0 }

    func applicationDidFinishLaunching(_ notification: Notification) {
        root = AppPaths.dataRoot
        guard FileManager.default.isExecutableFile(atPath: AppPaths.backend.path) else {
            return fatal("HFPilot is missing a required component. Reinstall the application.")
        }
        bridge = DesktopBridge(root: root)
        tunnel = TunnelController(root: root, restore: !CommandLine.arguments.contains("--resume-ui"))
        let state = root.appendingPathComponent(".work/mcp", isDirectory: true)
        do { try FileManager.default.createDirectory(at: state, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700]) }
        catch { return fatal("The application data folder could not be opened.") }
        lockFD = Darwin.open(state.appendingPathComponent("menu.lock").path, O_CREAT | O_RDWR, 0o600)
        guard lockFD >= 0, flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
            if lockFD >= 0 { Darwin.close(lockFD) }
            lockFD = -1
            NSApp.terminate(nil)
            return
        }
        menu.autoenablesItems = false
        menu.delegate = self
        let headerItem = NSMenuItem(title: "HFPilot status", action: nil, keyEquivalent: "")
        headerItem.isEnabled = false
        headerItem.view = statusHeader
        menu.addItem(headerItem)
        menu.addItem(.separator())
        toggleItem = add("Turn On", #selector(toggle), persistent: true)
        endItem = add("End Call…", #selector(endCall))
        endItem.isHidden = true
        menu.addItem(.separator())
        add("Conversations…", #selector(conversations), key: "1")
        add("Instructions…", #selector(instructions), key: "2")
        add("Settings…", #selector(settings), key: ",")
        menu.addItem(.separator())
        add("Quit HFPilot", #selector(quit), key: "q")
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.autosaveName = "VoiceAgentStatusItem"
        statusItem.menu = menu
        statusItem.button?.setAccessibilityLabel("HFPilot")
        installMainMenu()
        scheduleRefresh(5)
        render()
        refresh()
        bridge.data("overview") { [weak self] result in
            guard let self = self else { return }
            if result.code == 0 { self.setupData = result.object }
            if CommandLine.arguments.contains("--show-settings") { self.showLibrary(2) }
            else if CommandLine.arguments.contains("--show-window") { self.showLibrary(0) }
            if !CommandLine.arguments.contains("--resume-ui") {
                if self.readyForSetup() && CBManager.authorization == .allowedAlways {
                    self.perform("enable")
                } else {
                    self.showLibrary(2)
                    self.perform("local-only")
                }
            }
        }
    }

    @discardableResult private func add(_ title: String, _ action: Selector, key: String = "", persistent: Bool = false) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        menu.addItem(item)
        if persistent { (NSApp as? MenuApplication)?.persistentItems.insert(item) }
        return item
    }
    func menuWillOpen(_ menu: NSMenu) { (NSApp as? MenuApplication)?.trackedMenu = menu; scheduleRefresh(1); refresh() }
    func menuDidClose(_ menu: NSMenu) { (NSApp as? MenuApplication)?.trackedMenu = nil; scheduleRefresh(5) }
    private func scheduleRefresh(_ interval: TimeInterval) {
        timer?.invalidate()
        let timer = Timer(timeInterval: interval, repeats: true) { [weak self] _ in self?.refresh() }
        timer.tolerance = interval * 0.1
        self.timer = timer
        RunLoop.main.add(timer, forMode: .default)
        RunLoop.main.add(timer, forMode: .eventTracking)
        RunLoop.main.add(timer, forMode: .modalPanel)
    }

    private func render() {
        guard statusItem != nil else { return }
        let connected = runtime["bluetooth_connected"] as? Bool == true
        let listening = (runtime["listening"] as? [String: Any])?["status"] as? String ?? "paused"
        let phone = setupData["phone"] as? [String: Any]
        let name = runtime["phone_name"] as? String ?? phone?["name"] as? String ?? "iPhone"
        var symbol = "phone.down"
        var color = NSColor.secondaryLabelColor
        if operation == "pair" {
            stateLine.title = "Setting up phone…"
            phoneLine.title = "Follow the setup window"
            symbol = "antenna.radiowaves.left.and.right"
        } else if operation == "authorize" {
            stateLine.title = "Bluetooth permission needed"
            phoneLine.title = "Allow access in the macOS dialog"
            symbol = "exclamationmark.triangle"
        } else if quitRequested {
            stateLine.title = active ? "Quitting after this call" : "Saving results and quitting…"
            phoneLine.title = name
        } else if operation == "enable" || operation == "reconnect" || operation == "restart-server" {
            stateLine.title = "Connecting…"
            phoneLine.title = name
            symbol = "arrow.triangle.2.circlepath"
        } else if operation == "disable" {
            stateLine.title = active ? "Pausing after this call" : "Pausing…"
            phoneLine.title = name
        } else if !known {
            stateLine.title = "Checking status…"
            phoneLine.title = "Retrying automatically"
            symbol = "questionmark.circle"
        } else if active {
            stateLine.title = enabled ? "On a call" : "Pausing after this call"
            phoneLine.title = "\(name) · Connected"
            symbol = "phone.fill"
            color = .systemGreen
        } else if let error = runtime["hardware_error"] as? String ?? lastError {
            stateLine.title = "Needs attention"
            phoneLine.title = String(friendly(error).prefix(90))
            symbol = "exclamationmark.triangle"
        } else if enabled && connected && listening == "listening" {
            stateLine.title = "Answering calls"
            phoneLine.title = "\(name) · Connected"
            symbol = "phone.fill"
            color = .systemGreen
        } else if enabled {
            stateLine.title = listening == "starting" ? "Connecting…" : "Waiting for \(name)"
            phoneLine.title = "Reconnecting automatically"
            symbol = "phone.badge.waveform"
            color = .systemOrange
        } else {
            stateLine.title = "Paused"
            phoneLine.title = "Calls ring on your iPhone"
        }
        toggleItem.title = active ? (enabled ? "Pause After This Call" : "Keep Answering") : enabled ? "Pause Assistant" : "Turn On"
        toggleItem.isEnabled = operation == nil
        endItem.isHidden = !active
        endItem.isEnabled = operation == nil
        if symbol == "exclamationmark.triangle" { color = .systemOrange }
        statusHeader.update(title: stateLine.title, subtitle: phoneLine.title, color: color)
        if let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "HFPilot") { image.isTemplate = true; statusItem.button?.image = image }
        statusItem.button?.toolTip = "HFPilot — \(stateLine.title). \(phoneLine.title)"
        library?.update(runtime, operation: operation, status: stateLine.title, enabled: enabled, known: known)
        let conversationState = "\(runtime["active_call_id"] ?? "")-\(pending)"
        if conversationState != lastConversationState {
            lastConversationState = conversationState
            if library?.window?.isVisible == true && operation == nil { library?.refreshData() }
        }
        if quitRequested && quitAfterCall && !active && pending == 0 && operation == nil {
            quitAfterCall = false
            perform("stop")
        }
    }

    private func installMainMenu() {
        let main = NSMenu()
        let app = NSMenu()
        let appItem = NSMenuItem()
        appItem.submenu = app
        main.addItem(appItem)
        for (title, action, key) in [("Settings…", #selector(settings), ","), ("Quit HFPilot", #selector(quit), "q")] {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.target = self
            app.addItem(item)
        }
        let edit = NSMenu(title: "Edit")
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        editItem.submenu = edit
        main.addItem(editItem)
        for (title, action, key) in [("Undo", Selector(("undo:")), "z"), ("Redo", Selector(("redo:")), "Z"),
                                   ("Cut", #selector(NSText.cut(_:)), "x"), ("Copy", #selector(NSText.copy(_:)), "c"),
                                   ("Paste", #selector(NSText.paste(_:)), "v"), ("Select All", #selector(NSText.selectAll(_:)), "a")] {
            edit.addItem(NSMenuItem(title: title, action: action, keyEquivalent: key))
        }
        NSApp.mainMenu = main
    }

    private func refresh() {
        guard !refreshing else { return }
        refreshing = true
        let version = epoch
        guard let token = try? String(contentsOf: root.appendingPathComponent(".work/mcp/local-owner-token"), encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines), !token.isEmpty else { refreshProcesses(version); return }
        var request = URLRequest(url: URL(string: "http://127.0.0.1:8765/local/status")!)
        request.httpMethod = "POST"
        request.timeoutInterval = 2
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        session.dataTask(with: request) { [weak self] data, response, error in
            RunLoop.main.perform(inModes: [.default, .eventTracking, .modalPanel]) {
                guard let self = self else { return }
                guard version == self.epoch else { self.refreshing = false; self.refresh(); return }
                if error == nil, (response as? HTTPURLResponse)?.statusCode == 200, let data = data,
                   let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.runtime = value
                    self.serverRunning = true
                    if !self.quitRequested { self.tunnel.localServerAvailable() }
                    self.known = true
                    self.refreshing = false
                    self.render()
                } else { self.refreshProcesses(version) }
            }
        }.resume()
    }
    private func refreshProcesses(_ version: Int) {
        bridge.run("status") { [weak self] result in
            guard let self = self else { return }
            self.refreshing = false
            guard version == self.epoch else { self.refresh(); return }
            self.known = result.code == 0
            if self.known {
                self.serverRunning = result.object["server"] as? Bool == true
                self.runtime = result.object["assistant"] as? [String: Any] ?? [:]
                if self.serverRunning && !self.quitRequested { self.tunnel.localServerAvailable() }
            }
            self.render()
        }
    }

    private func perform(_ command: String) {
        guard operation == nil else { return }
        if command == "enable" && !readyForSetup() {
            bridge.data("overview") { [weak self] result in
                guard let self = self else { return }
                if result.code == 0 { self.setupData = result.object }
                if self.readyForSetup() { self.perform("enable") }
                else { self.showLibrary(2) }
            }
            return
        }
        if ["enable", "reconnect", "pair"].contains(command) && CBManager.authorization != .allowedAlways {
            if CBManager.authorization == .notDetermined {
                operation = "authorize"
                permissionAction = command
                permissionManager = CBCentralManager(delegate: self, queue: .main, options: [CBCentralManagerOptionShowPowerAlertKey: false])
                render()
            } else { fail("Allow HFPilot in System Settings → Privacy & Security → Bluetooth.") }
            return
        }
        if command == "pair" { showPhoneSetup(); return }
        if command == "enable" && quitAfterCall {
            quitRequested = false
            quitAfterCall = false
            NSApp.reply(toApplicationShouldTerminate: false)
        }
        operation = command
        epoch += 1
        lastError = nil
        library?.showMessage("")
        render()
        bridge.run(command) { [weak self] result in
            guard let self = self else { return }
            self.operation = nil
            self.epoch += 1
            if result.code != 0 {
                self.fail(result.error)
            }
            if result.code == 0 && command == "local-only" && self.readyForSetup() && !self.quitRequested {
                self.perform("enable")
                return
            }
            if result.code == 0 && command == "restart-server" && !self.quitRequested {
                self.perform("enable")
                return
            }
            if self.quitRequested && !self.quitAfterCall {
                if command == "stop" {
                    if result.code == 0 {
                        self.tunnel.shutdown { NSApp.reply(toApplicationShouldTerminate: true) }
                    } else {
                        NSApp.reply(toApplicationShouldTerminate: false)
                        self.quitRequested = false
                    }
                } else { self.perform("stop"); return }
            }
            self.refresh()
            self.library?.refreshData()
            self.render()
        }
    }
    private func readyForSetup() -> Bool {
        setupData["api_key_configured"] as? Bool == true && setupData["phone"] is [String: Any]
    }
    private func friendly(_ message: String) -> String {
        if message.contains("OPENAI_API_KEY") || message.contains(".env") { return "Review your OpenAI API key in Settings." }
        if message.localizedCaseInsensitiveContains("blueutil") || message.localizedCaseInsensitiveContains("permission") { return "Allow Bluetooth access in System Settings." }
        return message.replacingOccurrences(of: "RuntimeError: ", with: "").replacingOccurrences(of: "ValueError: ", with: "")
    }
    private func fail(_ message: String) {
        lastError = message
        showLibrary(2)
        library?.showMessage(friendly(message))
        render()
    }
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        guard CBManager.authorization != .notDetermined else { return }
        permissionManager = nil
        operation = nil
        if CBManager.authorization == .allowedAlways { perform(permissionAction) }
        else { fail("Allow HFPilot in System Settings → Privacy & Security → Bluetooth.") }
    }

    private func showLibrary(_ page: Int) {
        menu.cancelTracking()
        if library == nil {
            library = LibraryWindow(bridge: bridge, tunnel: tunnel, control: { [weak self] in self?.perform($0) },
                setupPhone: { [weak self] in self?.confirmPhoneSetup() },
                openBluetooth: { [weak self] in self?.openBluetoothSettings() })
        }
        library?.present(page: page)
        render()
        bridge.data("overview") { [weak self] result in if result.code == 0 { self?.setupData = result.object } }
    }
    private func confirmPhoneSetup() {
        guard !active && pending == 0 else { return }
        if setupData["phone"] is [String: Any] {
            let alert = NSAlert()
            alert.messageText = "Set up a phone?"
            alert.informativeText = "Use this to replace your phone or pair again after forgetting HFPilot. To reconnect your saved phone, choose Reconnect."
            alert.addButton(withTitle: "Cancel")
            alert.addButton(withTitle: "Set Up Phone")
            guard alert.runModal() == .alertSecondButtonReturn else { return }
        }
        perform("pair")
    }
    private func showPhoneSetup() {
        if phoneSetup?.isRunning != true {
            phoneSetup = PhoneSetupWindow(root: root, assistantEnabled: enabled, turnOn: { [weak self] in self?.perform("enable") }) { [weak self] running in
                guard let self = self else { return }
                self.operation = running ? "pair" : nil
                self.epoch += 1
                self.refresh()
                self.library?.refreshData()
                self.bridge.data("overview") { result in if result.code == 0 { self.setupData = result.object } }
                self.render()
                if !running && self.quitRequested { self.perform("stop") }
            }
        }
        phoneSetup?.present()
    }
    private func openBluetoothSettings() { NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth")!) }
    @objc private func toggle() { perform(enabled ? "disable" : "enable") }
    private func openLibraryFromMenu(_ page: Int) {
        // Let AppKit close the status menu and restore its previous key window
        // before requesting focus. A main-queue block can also run during tracking.
        RunLoop.main.perform(inModes: [.default]) { [weak self] in self?.showLibrary(page) }
    }
    @objc private func conversations() { openLibraryFromMenu(0) }
    @objc private func instructions() { openLibraryFromMenu(1) }
    @objc private func settings() { openLibraryFromMenu(2) }
    @objc private func quit() { NSApp.terminate(nil) }
    @objc private func endCall() {
        let alert = NSAlert()
        alert.messageText = "End the current call?"
        alert.informativeText = "The conversation will end immediately. Its transcript and result will be saved."
        alert.addButton(withTitle: "Cancel")
        alert.addButton(withTitle: "End Call")
        if alert.runModal() == .alertSecondButtonReturn { perform("end-call") }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if let library = library, library.window?.isVisible == true || library.window?.isMiniaturized == true {
            library.bringToFront()
        } else {
            showLibrary(0)
        }
        return false
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard lockFD >= 0, statusItem != nil else { return .terminateNow }
        if !quitRequested && library?.hasUnsavedInstructions == true {
            let alert = NSAlert()
            alert.messageText = "Your instruction changes are not saved"
            alert.informativeText = "Keep editing to save your draft before quitting."
            alert.addButton(withTitle: "Keep Editing")
            alert.addButton(withTitle: "Discard and Quit")
            if alert.runModal() == .alertFirstButtonReturn { showLibrary(1); return .terminateCancel }
        }
        if active && !quitRequested {
            let alert = NSAlert()
            alert.messageText = "A conversation is in progress"
            alert.informativeText = "You can finish this call before quitting HFPilot."
            alert.addButton(withTitle: "Finish Call and Quit")
            alert.addButton(withTitle: "Cancel")
            alert.addButton(withTitle: "End Call and Quit")
            let response = alert.runModal()
            if response == .alertSecondButtonReturn { return .terminateCancel }
            quitAfterCall = response == .alertFirstButtonReturn
        }
        quitRequested = true
        if phoneSetup?.isRunning == true { phoneSetup?.cancel(); return .terminateLater }
        if operation == nil { perform(quitAfterCall ? "disable" : "stop") }
        render()
        return .terminateLater
    }
    func applicationWillTerminate(_ notification: Notification) {
        tunnel?.closeParentPipe()
        timer?.invalidate()
        session.invalidateAndCancel()
        if lockFD >= 0 { Darwin.close(lockFD) }
    }
    private func fatal(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "HFPilot"
        alert.informativeText = message
        alert.runModal()
        NSApp.terminate(nil)
    }
}
