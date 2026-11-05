import Foundation

enum AppPaths {
    static var dataRoot: URL {
        if let override = ProcessInfo.processInfo.environment["HFPILOT_DATA_ROOT"], !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("HFPilot", isDirectory: true)
    }
    static var backend: URL {
        Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/HFPilotBackend")
    }
    static var help: URL? { Bundle.main.url(forResource: "Connections", withExtension: "html") }
    static var tunnelLauncher: URL? { Bundle.main.url(forResource: "OpenAI Tunnel", withExtension: "command") }
}
