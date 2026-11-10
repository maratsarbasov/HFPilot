import Cocoa

struct DesktopResult {
    let code: Int32
    let data: Data
    var object: [String: Any] { (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:] }
    var error: String {
        if let message = object["error"] as? String { return message }
        let lines = String(data: data, encoding: .utf8)?.split(separator: "\n") ?? []
        return lines.last.map(String.init) ?? "The operation could not finish. Try again."
    }
}

final class DesktopOperation {
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false

    func start(_ process: Process) throws -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard !cancelled else { return false }
        try process.run()
        self.process = process
        return true
    }
    func finish() { lock.lock(); process = nil; lock.unlock() }
    func cancel() {
        lock.lock(); defer { lock.unlock() }
        guard !cancelled else { return }
        cancelled = true
        if let process, process.isRunning { process.terminate() }
    }
}

final class DesktopBridge {
    let root: URL
    init(root: URL) { self.root = root }

    @discardableResult func run(_ command: String, payload: [String: Any]? = nil,
                               completion: @escaping (DesktopResult) -> Void) -> DesktopOperation {
        let operation = DesktopOperation()
        DispatchQueue.global(qos: .utility).async {
            let process = Process()
            process.executableURL = AppPaths.backend
            process.arguments = ["bench", command]
            process.currentDirectoryURL = self.root
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            environment["HFPILOT_DATA_ROOT"] = self.root.path
            environment.removeValue(forKey: "PYTHONHOME")
            environment.removeValue(forKey: "PYTHONPATH")
            process.environment = environment
            let output = Pipe()
            process.standardOutput = output
            process.standardError = output
            let input = Pipe()
            process.standardInput = payload == nil ? FileHandle.nullDevice : input
            let result: DesktopResult
            do {
                if try operation.start(process) {
                    if let payload = payload {
                        let data = try JSONSerialization.data(withJSONObject: payload)
                        try input.fileHandleForWriting.write(contentsOf: data)
                        try input.fileHandleForWriting.close()
                    }
                    let data = output.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()
                    result = DesktopResult(code: process.terminationStatus, data: data)
                } else {
                    result = DesktopResult(code: -999, data: Data())
                }
            } catch {
                try? input.fileHandleForWriting.close()
                if process.isRunning { process.terminate(); process.waitUntilExit() }
                result = DesktopResult(code: -1, data: Data("The local application could not start this operation.".utf8))
            }
            operation.finish()
            RunLoop.main.perform(inModes: [.default, .eventTracking, .modalPanel]) { completion(result) }
        }
        return operation
    }

    @discardableResult func data(_ action: String, values: [String: Any] = [:],
                                completion: @escaping (DesktopResult) -> Void) -> DesktopOperation {
        return run("desktop", payload: values.merging(["action": action]) { _, new in new }, completion: completion)
    }
}
