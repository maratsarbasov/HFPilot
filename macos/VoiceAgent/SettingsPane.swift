import SwiftUI

// The form receives display data only. Credentials stay in the secure AppKit sheet.
struct SettingsDisplay: Equatable {
    var status = "Checking status…"
    var enabled = false
    var canToggle = false
    var activeCall = false
    var recordAudio = false
    var canToggleRecording = false
    var recordingDetail = "Checking recording settings…"
    var phone = "iPhone"
    var phoneStatus = "Checking…"
    var savedPhone = false
    var serverRunning: Bool?
    var publicURL: String?
    var suggestedURL: String?
    var hasKey = false
    var note: String?
}

final class SettingsPresentation: ObservableObject {
    @Published var display = SettingsDisplay()
}

struct SettingsPane: View {
    @ObservedObject var presentation: SettingsPresentation
    @ObservedObject var tunnel: TunnelController
    let bridge: DesktopBridge
    let didUpdateConnections: ([String: Any]) -> Void
    @State private var showingConnections = false
    enum Action { case enable(Bool), recording(Bool), phone, key, log, configuration }
    let perform: (Action) -> Void

    private var state: SettingsDisplay { presentation.display }
    private func caption(_ title: String, _ detail: String, symbol: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).font(.system(size: 17)).foregroundStyle(.secondary)
                .frame(width: 23).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }.padding(.vertical, 3)
    }
    var body: some View {
        Form {
            Section("Assistant") {
                Toggle(isOn: Binding(get: { state.enabled }, set: { perform(.enable($0)) })) {
                    caption("Answer incoming calls", state.activeCall && state.enabled ? "Pausing will let this call finish." : state.status, symbol: "waveform")
                }.toggleStyle(.switch).disabled(!state.canToggle)
                Toggle(isOn: Binding(get: { state.recordAudio }, set: { perform(.recording($0)) })) {
                    caption("Record call audio", state.recordingDetail, symbol: "waveform.circle")
                }.toggleStyle(.switch).disabled(!state.canToggleRecording)
                LabeledContent {
                    Button(state.hasKey ? "Change…" : "Add Key…") { perform(.key) }
                } label: {
                    caption("OpenAI API key", state.hasKey ? "Key saved on this Mac" : "Required to answer calls", symbol: "key")
                }
            }
            Section("Connections") {
                LabeledContent {
                    Button(state.savedPhone ? "Manage…" : "Set Up…") { perform(.phone) }
                } label: { caption(state.phone, state.phoneStatus, symbol: "iphone") }
                LabeledContent {
                    Button("Details…") { showingConnections = true }
                } label: { caption("Assistant connections", tunnel.isRunning ? "OpenAI Tunnel · \(tunnel.state.capitalized)" : (state.publicURL == nil ? "Local MCP server" : "Public address saved"), symbol: "cable.connector") }
            }
            if let note = state.note { Text(note).font(.callout).foregroundStyle(.secondary) }
            Section("Local files") {
                LabeledContent("Activity log") {
                    Button("Open Log") { perform(.log) }
                }
                LabeledContent("Agent configuration") {
                    Button("Open Configuration…") { perform(.configuration) }
                }
            }
        }.formStyle(.grouped)
        .sheet(isPresented: $showingConnections) {
            AssistantConnections(presentation: presentation, tunnel: tunnel, bridge: bridge, didUpdate: didUpdateConnections)
        }
    }
}
