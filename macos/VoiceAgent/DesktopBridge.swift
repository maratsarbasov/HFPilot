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

final class DesktopBridge {
    let root: URL
    init(root: URL) { self.root = root }

    func run(_ command: String, payload: [String: Any]? = nil, completion: @escaping (DesktopResult) -> Void) {
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
                try process.run()
                if let payload = payload {
                    let data = try JSONSerialization.data(withJSONObject: payload)
                    try input.fileHandleForWriting.write(contentsOf: data)
                    try input.fileHandleForWriting.close()
                }
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                result = DesktopResult(code: process.terminationStatus, data: data)
            } catch {
                try? input.fileHandleForWriting.close()
                if process.isRunning { process.terminate(); process.waitUntilExit() }
                result = DesktopResult(code: -1, data: Data("The local application could not start this operation.".utf8))
            }
            RunLoop.main.perform(inModes: [.default, .eventTracking, .modalPanel]) { completion(result) }
        }
    }

    func data(_ action: String, values: [String: Any] = [:], completion: @escaping (DesktopResult) -> Void) {
        run("desktop", payload: values.merging(["action": action]) { _, new in new }, completion: completion)
    }
}
