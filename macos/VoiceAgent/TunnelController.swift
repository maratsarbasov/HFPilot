import Cocoa
import Combine

/// Owned by the application, so closing a window never interrupts the connection.
final class TunnelController: ObservableObject {
    @Published var tunnelID = ""
    @Published private(set) var state = "stopped"
    @Published private(set) var message = "Disconnected"
    @Published private(set) var isRunning = false
    private let root: URL
    private let settings: URL
    private var process: Process?
    private var input: Pipe?
    private var enabled = false
    private var restoreAttempted: Bool
    private var restart = false
    private var quitting = false
    private var onStopped: (() -> Void)?

    init(root: URL, restore: Bool) {
        self.root = root
        settings = root.appendingPathComponent(".work/mcp/openai-tunnel.json")
        restoreAttempted = !restore
        if let data = try? Data(contentsOf: settings),
           let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            tunnelID = value["tunnel_id"] as? String ?? ""
            enabled = value["enabled"] as? Bool == true
        }
    }

    func localServerAvailable() {
        guard !restoreAttempted, !quitting else { return }
        restoreAttempted = true
        if enabled { connect() }
    }

    private func save() throws {
        let data = try JSONSerialization.data(withJSONObject: ["tunnel_id": tunnelID, "enabled": enabled])
        try data.write(to: settings, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: settings.path)
    }

    func connect() {
        guard process == nil, !quitting else { return }
        restoreAttempted = true
        tunnelID = tunnelID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard tunnelID.range(of: "^tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}$", options: .regularExpression) != nil else {
            state = "error"; message = "Enter the complete Tunnel ID from OpenAI Platform."; return
        }
        enabled = true
        do { try save() } catch {
            enabled = false; state = "error"; message = "The tunnel settings could not be saved."; return
        }
        let child = Process()
        child.executableURL = AppPaths.backend
        child.arguments = ["tunnel-managed"]
        child.currentDirectoryURL = root
        var environment = ProcessInfo.processInfo.environment
        environment["HFPILOT_DATA_ROOT"] = root.path
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        for name in ["PYTHONHOME", "PYTHONPATH", "OPENAI_API_KEY", "CONTROL_PLANE_API_KEY"] {
            environment.removeValue(forKey: name)
        }
        child.environment = environment
        let parent = Pipe(), output = Pipe()
        child.standardInput = parent
        child.standardOutput = output
        child.standardError = FileHandle.nullDevice
        process = child; input = parent; isRunning = true
        state = "connecting"; message = "Starting tunnel-client…"
        do {
            try child.run()
            var request = try JSONSerialization.data(withJSONObject: ["tunnel_id": tunnelID])
            request.append(0x0a)
            try parent.fileHandleForWriting.write(contentsOf: request)
        } catch {
            try? parent.fileHandleForWriting.close()
            if child.isRunning { child.terminate() }
            process = nil; input = nil; isRunning = false
            state = "error"; message = "The tunnel could not start. Reinstall HFPilot and try again."
            return
        }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            var buffer = Data()
            while true {
                let chunk = output.fileHandleForReading.availableData
                if chunk.isEmpty { break }
                buffer.append(chunk)
                while let end = buffer.firstIndex(of: 0x0a) {
                    let line = buffer[..<end]
                    buffer.removeSubrange(...end)
                    guard let value = try? JSONSerialization.jsonObject(with: line) as? [String: String],
                          let state = value["state"], let message = value["message"] else { continue }
                    RunLoop.main.perform(inModes: [.default, .eventTracking, .modalPanel]) { [weak self] in
                        guard let self, self.process === child, self.state != "stopping" else { return }
                        self.state = state; self.message = message
                    }
                }
                if buffer.count > 8192 { buffer.removeAll() }
            }
            child.waitUntilExit()
            try? output.fileHandleForReading.close()
            RunLoop.main.perform(inModes: [.default, .eventTracking, .modalPanel]) { [weak self] in
                guard let self, self.process === child else { return }
                try? self.input?.fileHandleForWriting.close()
                self.process = nil; self.input = nil; self.isRunning = false
                if self.state == "stopping" || self.state == "stopped" {
                    self.state = "stopped"; self.message = "Disconnected"
                } else if self.state != "error" {
                    self.state = "error"; self.message = "The tunnel stopped. Connect again to retry."
                }
                let completion = self.onStopped; self.onStopped = nil
                completion?()
                if self.restart && !self.quitting { self.restart = false; self.connect() }
            }
        }
    }

    func disconnect() {
        enabled = false; restart = false
        do { try save() } catch {
            state = "error"; message = "The disconnect preference could not be saved. Try again."; return
        }
        stop()
    }

    func credentialsChanged() {
        if isRunning { restart = true; stop() }
    }

    private func stop() {
        guard process != nil else { state = "stopped"; message = "Disconnected"; return }
        state = "stopping"; message = "Disconnecting…"
        // EOF is also delivered automatically if the application crashes.
        try? input?.fileHandleForWriting.close()
    }

    func shutdown(completion: @escaping () -> Void) {
        quitting = true; restart = false
        guard process != nil else { completion(); return }
        onStopped = completion
        stop()
    }

    func closeParentPipe() { try? input?.fileHandleForWriting.close() }
}
