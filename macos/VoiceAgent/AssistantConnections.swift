import SwiftUI

private extension View {
    func connectionCard() -> some View {
        background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.7))
    }
}

private enum ConnectionMethod: String, CaseIterable, Identifiable {
    case openai, tailscale, quick, cloudflare
    var id: String { rawValue }
    var name: String {
        switch self {
        case .openai: return "OpenAI Tunnel"
        case .tailscale: return "Tailscale Funnel"
        case .quick: return "Cloudflare Quick Tunnel"
        case .cloudflare: return "Cloudflare Tunnel"
        }
    }
    var detail: String {
        switch self {
        case .openai: return "A private connection for ChatGPT and other supported OpenAI products."
        case .tailscale: return "A permanent public address without your own domain."
        case .quick: return "A temporary public address for testing. It changes when restarted."
        case .cloudflare: return "A permanent public address on your own domain."
        }
    }
    var website: URL {
        switch self {
        case .openai: return URL(string: "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels")!
        case .tailscale: return URL(string: "https://tailscale.com/download/mac")!
        case .quick: return URL(string: "https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/")!
        case .cloudflare: return URL(string: "https://developers.cloudflare.com/tunnel/get-started/")!
        }
    }
    var command: String {
        switch self {
        case .tailscale: return "tailscale funnel --bg 8765"
        case .quick: return "cloudflared tunnel --url http://127.0.0.1:8765"
        case .cloudflare: return "http://127.0.0.1:8765"
        case .openai: return ""
        }
    }
}

struct AssistantConnections: View {
    @ObservedObject var presentation: SettingsPresentation
    @ObservedObject var tunnel: TunnelController
    let bridge: DesktopBridge
    let didUpdate: ([String: Any]) -> Void
    @Environment(\.dismiss) private var dismiss
    @Environment(\.colorScheme) private var scheme
    @AppStorage("assistantConnectionMethod") private var methodID = ConnectionMethod.openai.rawValue
    @State private var editing = false
    @State private var input = ""
    @State private var busy = false
    @State private var feedback = ""
    @State private var error: String?
    @State private var client: URL?
    @FocusState private var inputFocused: Bool
    private var display: SettingsDisplay { presentation.display }
    private var method: ConnectionMethod { ConnectionMethod(rawValue: methodID) ?? .openai }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 5) {
                Text(editing ? "Public address" : "Connect an assistant").font(.system(size: 17, weight: .semibold))
                detail(editing ? "Use the HTTPS address provided by your tunnel." : "Use HFPilot from ChatGPT or another assistant.")
            }
            if editing { editor } else {
                localConnection
                ViewThatFits(in: .vertical) {
                    content.fixedSize(horizontal: false, vertical: true)
                    ScrollView { content.padding(.trailing, 6) }
                }
                .clipShape(RoundedRectangle(cornerRadius: 10))
                .connectionCard()
            }
            Divider()
            HStack(spacing: 8) {
                Button { if let url = AppPaths.help { NSWorkspace.shared.open(url) } } label: {
                    Image(systemName: "questionmark.circle")
                }.buttonStyle(.plain).help("Connection setup help").accessibilityLabel("Connection setup help")
                if let error { Text(error).font(.caption).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
                else { detail(feedback) }
                Spacer(minLength: 0)
                Button(editing ? "Cancel" : "Done") {
                    if editing { editing = false; error = nil } else { dismiss() }
                }.keyboardShortcut(editing ? .cancelAction : .defaultAction).disabled(busy)
                if editing {
                    Button("Save Address") { update("save", input) }.keyboardShortcut(.defaultAction)
                        .disabled(busy || input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        .padding(24).frame(width: 584)
        .frame(maxHeight: max(400, (NSScreen.main?.visibleFrame.height ?? 800) - 80))
        .fixedSize(horizontal: true, vertical: true)
        .background(scheme == .dark ? Color(white: 0.155) : Color(red: 0.965, green: 0.961, blue: 0.957))
        .onAppear { client = TunnelInstallation.selected(in: bridge.root) }
        .onReceive(Timer.publish(every: 2, on: .main, in: .common).autoconnect()) { _ in
            client = TunnelInstallation.selected(in: bridge.root)
        }
        .onChange(of: methodID) { _ in feedback = ""; error = nil }
    }

    private func detail(_ text: String) -> some View {
        Text(text).font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
    }
    private func step<Content: View>(_ number: Int, _ title: String, _ text: String,
                                     @ViewBuilder actions: () -> Content) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text("\(number)").font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
                .frame(width: 18).padding(.top, 1).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.system(size: 13, weight: .semibold))
                detail(text)
                actions().controlSize(.small)
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }
    private var localConnection: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("On this Mac").font(.system(size: 12, weight: .semibold))
                Spacer()
                Circle().fill(display.serverRunning == true ? Color.green : Color.secondary).frame(width: 7, height: 7)
                detail(display.serverRunning.map { $0 ? "Server running" : "Server stopped" } ?? "Checking…")
            }
            VStack(spacing: 0) {
                copyRow("http://127.0.0.1:8765/mcp", label: "Copy URL")
                    .padding(.horizontal, 13).padding(.vertical, 8)
                Divider()
                HStack {
                    Label("Access code", systemImage: "key").foregroundStyle(.secondary)
                    Spacer()
                    Button("Copy Access Code", action: copyCode).buttonStyle(.link)
                }.font(.system(size: 12)).padding(.horizontal, 13).padding(.vertical, 8)
            }.connectionCard()
        }
    }
    private var content: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 7) {
                Picker("Connect using", selection: $methodID) {
                    ForEach(ConnectionMethod.allCases) { Text($0.name).tag($0.rawValue) }
                }.pickerStyle(.menu)
                detail(method.detail)
            }
            Divider()
            if method == .openai { openAISetup } else { publicSetup }
        }.padding(14)
    }
    private var openAISetup: some View {
        VStack(alignment: .leading, spacing: 14) {
            step(1, "Install tunnel-client", "Run this in Terminal. HFPilot finds the installed client automatically.") {
                copyRow("brew install openai/tools/tunnel-client", label: "Copy")
                HStack(spacing: 12) {
                    Link("Get Homebrew…", destination: URL(string: "https://brew.sh")!)
                    Button("Choose File…", action: chooseClient).buttonStyle(.link)
                        .help("Use a client installed in a custom location")
                    if let client { Label("Client found", systemImage: "checkmark.circle").font(.caption).foregroundStyle(.secondary).help(client.path) }
                }
            }
            step(2, "Create your OpenAI tunnel", "Link the tunnel to your ChatGPT workspace and copy its Tunnel ID.") {
                Link("Open OpenAI Platform…", destination: URL(string: "https://platform.openai.com/settings/organization/tunnels")!)
            }
            step(3, "Connect HFPilot", "Uses the OpenAI API key saved in Settings. Give that key Tunnels Read + Use access as well as model access.") {
                HStack(spacing: 8) {
                    TextField("Tunnel ID", text: $tunnel.tunnelID)
                        .textFieldStyle(.roundedBorder).disabled(tunnel.isRunning)
                        .help("Paste the complete ID, starting with tunnel_")
                    if tunnel.isRunning {
                        Button("Disconnect") { tunnel.disconnect() }.disabled(tunnel.state == "stopping")
                    } else {
                        Button("Connect", action: connectTunnel)
                            .disabled(client == nil || display.serverRunning != true || !display.hasKey || tunnel.tunnelID.isEmpty)
                    }
                }
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: tunnel.state == "connected" ? "checkmark.circle.fill" : (tunnel.state == "error" ? "exclamationmark.circle" : "circle"))
                        .foregroundStyle(tunnel.state == "connected" ? Color.green : .secondary)
                    detail(tunnel.message)
                }.font(.caption)
                if !display.hasKey { detail("Add your OpenAI API key in Settings first.") }
                else if display.serverRunning != true { detail("Start HFPilot’s local server before connecting.") }
                Link("Edit API Key Permissions…", destination: URL(string: "https://platform.openai.com/settings/organization/api-keys")!)
                detail("You can close this window. Connects again when HFPilot opens; Disconnect turns this off. Quitting HFPilot stops the tunnel.")
            }
            step(4, "Add HFPilot to ChatGPT", "Choose Tunnel and No authentication, then select your tunnel. Ask ChatGPT for HFPilot’s status.") {
                Link("Open ChatGPT Plugins…", destination: URL(string: "https://chatgpt.com/plugins")!)
            }
        }
    }
    private var publicSetup: some View {
        VStack(alignment: .leading, spacing: 18) {
            step(1, "Set up \(method.name)", method == .cloudflare ? "Create a named tunnel and choose a hostname on your domain." : "Install the provider’s app and follow its setup instructions.") {
                Link("Open Setup Guide…", destination: method.website)
            }
            step(2, method == .cloudflare ? "Set the local service address" : "Start your tunnel in Terminal", method == .cloudflare ? "Point your hostname to this local service." : "Run this command and follow the provider’s prompts.") {
                copyRow(method.command, label: method == .cloudflare ? "Copy Address" : "Copy Command")
            }
            step(3, "Connect your assistant", "Add /mcp to your public HTTPS address and enter it in your assistant. Save the same address here to enable sign-in.") {
                Button(display.publicURL == nil ? "Enter Public Address…" : "Change Public Address…") {
                    input = display.publicURL ?? ""; editing = true; inputFocused = true; error = nil; feedback = ""
                }
            }
            if let candidate = display.suggestedURL {
                VStack(alignment: .leading, spacing: 7) {
                    Text("Confirm this address?").font(.callout.weight(.semibold))
                    Text(verbatim: candidate).font(.caption).textSelection(.enabled)
                    detail("Confirm only if it matches the address you added to your assistant.")
                    HStack {
                        Button("Dismiss") { update("dismiss", candidate) }
                        Button("Use Address") { update("confirm", candidate) }
                    }.disabled(busy).controlSize(.small)
                }
            }
            if let url = display.publicURL { copyRow(url, label: "Copy URL") }
        }
    }
    private func copyRow(_ value: String, label: String) -> some View {
        HStack(spacing: 8) {
            Text(verbatim: value).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            Button(label) { copy(value, message: "Copied") }.controlSize(.small)
        }
    }
    private var editor: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("https://calls.example.com/mcp", text: $input).focused($inputFocused)
                .onSubmit { if !busy { update("save", input) } }
            detail("Your assistant uses this address when asking you to approve access.")
        }
    }
    private func chooseClient() {
        let panel = NSOpenPanel()
        panel.title = "Select tunnel-client"
        panel.message = "Choose an existing tunnel-client executable installed in a custom location."
        panel.prompt = "Select"
        panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            guard url.lastPathComponent == "tunnel-client", FileManager.default.isExecutableFile(atPath: url.path) else {
                error = "Select the extracted tunnel-client executable, not its ZIP archive."; return
            }
            do { try TunnelInstallation.save(url, in: bridge.root); client = url; error = nil; feedback = "Client selected" }
            catch { self.error = "The selected location could not be saved." }
        }
    }
    private func connectTunnel() {
        guard let client else { return }
        do { try TunnelInstallation.save(client, in: bridge.root) }
        catch { self.error = "The selected location could not be saved."; return }
        error = nil; feedback = ""
        tunnel.connect()
    }
    private func copy(_ value: String, message: String) {
        NSPasteboard.general.clearContents(); error = nil
        feedback = NSPasteboard.general.setString(value, forType: .string) ? message : "Could not copy"
    }
    private func copyCode() {
        guard let code = try? String(contentsOf: bridge.root.appendingPathComponent(".work/mcp/token"), encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines), !code.isEmpty else {
            error = "Start HFPilot’s local server to create an access code."; return
        }
        copy(code, message: "Access code copied")
    }
    private func update(_ command: String, _ url: String) {
        guard !busy else { return }
        busy = true; error = nil; feedback = ""
        bridge.data("connection_address", values: ["command": command, "url": url]) { result in
            busy = false
            guard result.code == 0 else { error = result.error; return }
            didUpdate(result.object); editing = false
            if command != "dismiss" { feedback = "Address saved" }
        }
    }
}
