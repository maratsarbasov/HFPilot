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
    var voice = VoiceSettings()
}

final class SettingsPresentation: ObservableObject {
    @Published var display = SettingsDisplay()
}

struct SettingsPane: View {
    @ObservedObject var presentation: SettingsPresentation
    @ObservedObject var tunnel: TunnelController
    let bridge: DesktopBridge
    let didUpdateConnections: ([String: Any]) -> Void
    let didUpdateVoice: ([String: Any]) -> Void
    @State private var showingConnections = false
    @State private var showingVoices = false
    enum Action { case enable(Bool), recording(Bool), phone, key, log, configuration }
    let perform: (Action) -> Void

    private var state: SettingsDisplay { presentation.display }
    private func row<Control: View>(_ title: String, detail: String? = nil, symbol: String,
                                    @ViewBuilder control: () -> Control) -> some View {
        HStack(spacing: 16) {
            HStack(spacing: 10) {
                Image(systemName: symbol)
                    .font(.system(size: 16)).foregroundStyle(.secondary)
                    .frame(width: 20).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(.system(size: 13, weight: .medium))
                    if let detail {
                        Text(detail).font(.system(size: 12)).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
            control().fixedSize().controlSize(.regular)
        }
        .frame(minHeight: 24)
        .padding(.horizontal, 14).padding(.vertical, 10)
    }

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.system(size: 13, weight: .semibold)).accessibilityAddTraits(.isHeader)
            VStack(spacing: 0, content: content)
                .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(Color(nsColor: .separatorColor).opacity(0.5), lineWidth: 0.5))
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    private var divider: some View { Divider().padding(.horizontal, 14) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                section("Assistant") {
                    row("Assistant voice", detail: state.voice.error ?? (state.voice.name.isEmpty ? "Loading voices…" :
                        state.voice.model != "gpt-live-1" ? "Voice selection is available for gpt-live-1" :
                        "\(state.voice.name) · Used for calls and tests"), symbol: "person.wave.2") {
                        Button("Choose Voice…") { showingVoices = true }.disabled(!state.voice.canChoose)
                    }
                    divider
                    row("Answer incoming calls", detail: state.activeCall && state.enabled ?
                        "Pausing will let this call finish." : state.status, symbol: "waveform") {
                        Toggle("Answer incoming calls", isOn: Binding(get: { state.enabled }, set: { perform(.enable($0)) }))
                            .labelsHidden().toggleStyle(.switch).disabled(!state.canToggle)
                    }
                    divider
                    row("Record call audio", detail: state.recordingDetail, symbol: "waveform.circle") {
                        Toggle("Record call audio", isOn: Binding(get: { state.recordAudio }, set: { perform(.recording($0)) }))
                            .labelsHidden().toggleStyle(.switch).disabled(!state.canToggleRecording)
                    }
                    divider
                    row("OpenAI API key", detail: state.hasKey ? "Key saved on this Mac" : "Required for calls and tests", symbol: "key") {
                        Button(state.hasKey ? "Change…" : "Add Key…") { perform(.key) }
                    }
                }
                section("Connections") {
                    row(state.phone, detail: state.phoneStatus, symbol: "iphone") {
                        Button(state.savedPhone ? "Manage…" : "Set Up…") { perform(.phone) }
                    }
                    divider
                    row("Assistant connections", detail: tunnel.isRunning ? "OpenAI Tunnel · \(tunnel.state.capitalized)" :
                        (state.publicURL == nil ? "Local MCP server" : "Public address saved"), symbol: "cable.connector") {
                        Button("Details…") { showingConnections = true }
                    }
                }
                section("Local files") {
                    row("Activity log", symbol: "doc.text") {
                        Button("Open Log") { perform(.log) }
                    }
                    divider
                    row("Agent configuration", symbol: "slider.horizontal.3") {
                        Button("Open Configuration…") { perform(.configuration) }
                    }
                }
                if let note = state.note, !note.isEmpty {
                    Text(note).font(.system(size: 12)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.horizontal, 24).padding(.vertical, 20)
            .frame(maxWidth: .infinity, alignment: .topLeading)
        }
        .sheet(isPresented: $showingConnections) {
            AssistantConnections(presentation: presentation, tunnel: tunnel, bridge: bridge, didUpdate: didUpdateConnections)
        }
        .sheet(isPresented: $showingVoices) {
            VoicePicker(presentation: presentation, bridge: bridge, didSave: didUpdateVoice)
        }
    }
}
