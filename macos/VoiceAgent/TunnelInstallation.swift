import Cocoa

// This records only the location of software the user installed separately.
// It neither installs tunnel-client nor owns its process lifetime.
enum TunnelInstallation {
    static func selected(in root: URL) -> URL? {
        if let path = try? String(contentsOf: root.appendingPathComponent(".work/mcp/tunnel-client-path"), encoding: .utf8) {
            let url = URL(fileURLWithPath: path.trimmingCharacters(in: .whitespacesAndNewlines))
            if FileManager.default.isExecutableFile(atPath: url.path) { return url }
        }
        let candidates = ["/opt/homebrew/bin/tunnel-client", "/usr/local/bin/tunnel-client",
                          FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/tunnel-client").path]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }.map { URL(fileURLWithPath: $0) }
    }

    static func save(_ url: URL, in root: URL) throws {
        let directory = root.appendingPathComponent(".work/mcp", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let file = directory.appendingPathComponent("tunnel-client-path")
        try (url.path + "\n").write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
    }
}
