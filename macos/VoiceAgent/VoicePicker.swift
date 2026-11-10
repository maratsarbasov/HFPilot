import SwiftUI
import AVFoundation

struct VoiceOption: Decodable, Identifiable, Equatable {
    let id: String
    let name: String
    let language: String
    let accent: String
    let presentation: String
    let group: String

    var detail: String {
        if group == "Original voices" { return "Original voice" }
        let origin = [accent, language].filter { !$0.isEmpty }.joined(separator: " ")
        return [presentation, origin].filter { !$0.isEmpty }.joined(separator: " · ")
    }
    static let all: [VoiceOption] = {
        struct Catalog: Decodable { let voices: [VoiceOption] }
        guard let url = Bundle.main.url(forResource: "catalog", withExtension: "json", subdirectory: "Voices"),
              let data = try? Data(contentsOf: url), let catalog = try? JSONDecoder().decode(Catalog.self, from: data) else { return [] }
        return catalog.voices
    }()
}

struct VoiceSettings: Equatable {
    var current = ""
    var model = ""
    var revision = ""
    var error: String?
    init(_ data: [String: Any] = [:]) {
        current = data["current"] as? String ?? ""
        model = data["model"] as? String ?? ""
        revision = data["revision"] as? String ?? ""
        error = data["error"] as? String
    }
    var name: String { VoiceOption.all.first { $0.id == current }?.name ?? current }
    var canChoose: Bool { model == "gpt-live-1" && !revision.isEmpty && error == nil }
}

final class VoiceSamplePlayer: NSObject, ObservableObject, AVAudioPlayerDelegate {
    @Published private(set) var playing: String?
    @Published private(set) var generating: String?
    @Published private(set) var error: String?
    private var player: AVAudioPlayer?
    private var operation: DesktopOperation?
    private var generation = UUID()
    private var quitObserver: NSObjectProtocol?

    override init() {
        super.init()
        quitObserver = NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification,
            object: nil, queue: .main) { [weak self] _ in self?.stop() }
    }
    deinit {
        operation?.cancel()
        if let quitObserver { NotificationCenter.default.removeObserver(quitObserver) }
    }

    func toggle(_ voice: VoiceOption, language: String, bridge: DesktopBridge) {
        if playing == voice.id || generating == voice.id { stop(); return }
        stop()
        if language == "English" {
            guard let url = Bundle.main.url(forResource: voice.id, withExtension: "m4a", subdirectory: "Voices/en"),
                  let data = try? Data(contentsOf: url) else {
                error = "This sample is missing. Reinstall HFPilot to restore voice previews."
                return
            }
            play(data, voice: voice.id)
            return
        }
        generating = voice.id
        let request = generation
        operation = bridge.data("voice_preview", values: ["voice": voice.id, "language": language]) { [weak self] result in
            guard let self, self.generation == request else { return }
            self.operation = nil
            self.generating = nil
            guard result.code == 0 else { self.error = result.error; return }
            guard let encoded = result.object["audio"] as? String, let data = Data(base64Encoded: encoded),
                  result.object["voice"] as? String == voice.id else {
                self.error = "The preview could not be generated. Try again."
                return
            }
            self.play(data, voice: voice.id)
        }
    }
    private func play(_ data: Data, voice: String) {
        do {
            let player = try AVAudioPlayer(data: data)
            player.delegate = self
            self.player = player
            guard player.play() else { throw NSError(domain: "VoiceSample", code: 1) }
            playing = voice
        } catch {
            stop()
            self.error = "The sample could not play. Check your default sound output and try again."
        }
    }
    func stop() {
        generation = UUID()
        operation?.cancel(); operation = nil
        player?.stop(); player = nil
        playing = nil; generating = nil; error = nil
    }
    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        stop()
        if !flag { error = "Playback stopped. Try the sample again." }
    }
    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        stop()
        self.error = "This sample could not be read. Try previewing this voice again."
    }
}

struct VoicePicker: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var presentation: SettingsPresentation
    let bridge: DesktopBridge
    let didSave: ([String: Any]) -> Void
    @StateObject private var sample = VoiceSamplePlayer()
    @State private var selected: String
    @State private var original: VoiceSettings
    @State private var query = ""
    @State private var languageFilter = "All voices"
    @State private var previewLanguage = "English"
    @State private var customLanguage = ""
    @State private var saving = false
    @State private var error: String?

    init(presentation: SettingsPresentation, bridge: DesktopBridge, didSave: @escaping ([String: Any]) -> Void) {
        self.presentation = presentation
        self.bridge = bridge
        self.didSave = didSave
        _selected = State(initialValue: presentation.display.voice.current)
        _original = State(initialValue: presentation.display.voice)
    }
    private var choices: [VoiceOption] {
        VoiceOption.all.filter { voice in
            (languageFilter == "All voices" || voice.language == languageFilter) &&
            (query.isEmpty || "\(voice.name) \(voice.detail) \(voice.language)".localizedCaseInsensitiveContains(query))
        }
    }
    private var selectedName: String { VoiceOption.all.first { $0.id == selected }?.name ?? selected }
    private var activeCall: Bool { presentation.display.activeCall }
    private var onlinePreview: Bool { previewLanguage != "English" }
    private var requestedLanguage: String {
        (previewLanguage == "Other" ? customLanguage : previewLanguage).trimmingCharacters(in: .whitespacesAndNewlines)
    }
    private var canPreview: Bool {
        !activeCall && !saving && (!onlinePreview || (presentation.display.hasKey && (2...80).contains(requestedLanguage.count)))
    }
    private static let languages = [
        ("English", "English"), ("Russian", "Русский"), ("Spanish", "Español"),
        ("French", "Français"), ("German", "Deutsch"), ("Portuguese", "Português"),
        ("Italian", "Italiano"), ("Arabic", "العربية"), ("Chinese", "中文"),
        ("Japanese", "日本語"), ("Korean", "한국어"), ("Hindi", "हिन्दी"),
        ("Ukrainian", "Українська"), ("Turkish", "Türkçe"), ("Other", "Other language…")
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 5) {
                Text("Choose a voice").font(.system(size: 21, weight: .semibold))
                Text("For incoming calls, outgoing calls and tests. Changes apply to the next call.")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Preview language").font(.system(size: 12, weight: .medium))
                    Text(onlinePreview ? "Generated on request · API usage applies" : "Instant English samples · Works offline")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer()
                Picker("Preview language", selection: $previewLanguage) {
                    ForEach(Self.languages, id: \.0) { Text($0.1).tag($0.0) }
                }.labelsHidden().frame(width: 190)
            }
            if previewLanguage == "Other" {
                TextField("Language, e.g. Dutch or Brazilian Portuguese", text: $customLanguage)
                    .textFieldStyle(.roundedBorder).accessibilityLabel("Custom preview language")
            }
            if onlinePreview {
                Text(presentation.display.hasKey ? "Preview only. Your microphone stays off; call language follows your instructions." :
                    "Add an OpenAI API key in Settings to generate previews in this language.")
                    .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 10) {
                TextField("Search voices or accents", text: $query).textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Search voices or accents")
                Picker("Voice origin", selection: $languageFilter) {
                    Text("All voices").tag("All voices")
                    ForEach(Array(Set(VoiceOption.all.map(\.language))).sorted(), id: \.self) { Text($0).tag($0) }
                }.labelsHidden().frame(width: 150)
            }
            ScrollView {
                if choices.isEmpty {
                    VStack(spacing: 10) {
                        Image(systemName: "magnifyingglass").font(.system(size: 25)).foregroundStyle(.secondary)
                        Text("No matching voices").font(.headline)
                        Button("Clear filters") { query = ""; languageFilter = "All voices" }
                    }.frame(maxWidth: .infinity).padding(.vertical, 60)
                } else {
                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible())], spacing: 10) {
                        ForEach(choices) { voice in voiceCard(voice) }
                    }.padding(2)
                }
            }
            .frame(maxHeight: .infinity)
            if activeCall {
                Text("Finish the current call to preview voices. You can still choose the voice for your next call.")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
            if let message = error ?? sample.error {
                Text(message).font(.system(size: 12)).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Current voice: \(presentation.display.voice.name)").font(.system(size: 12, weight: .medium))
                    Text("Previewing does not change your voice.").font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Cancel") { sample.stop(); dismiss() }.keyboardShortcut(.cancelAction).disabled(saving)
                Button(saving ? "Saving…" : "Use \(selectedName)") { save() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(saving || selected == original.current || !original.canChoose || !VoiceOption.all.contains { $0.id == selected })
            }
        }
        .padding(24).frame(width: 620, height: 560)
        .onAppear { refreshSelection() }
        .onChange(of: previewLanguage) { _, _ in sample.stop() }
        .onChange(of: customLanguage) { _, _ in sample.stop() }
        .onChange(of: query) { _, _ in sample.stop() }
        .onChange(of: languageFilter) { _, _ in sample.stop() }
        .onChange(of: activeCall) { _, active in if active { sample.stop() } }
        .onDisappear { sample.stop() }
    }

    private func voiceCard(_ voice: VoiceOption) -> some View {
        let chosen = selected == voice.id
        let playing = sample.playing == voice.id
        let generating = sample.generating == voice.id
        return VStack(alignment: .leading, spacing: 8) {
            Button {
                selected = voice.id
                error = nil
            } label: {
                HStack(alignment: .top, spacing: 9) {
                    Image(systemName: chosen ? "checkmark.circle.fill" : "circle")
                        .font(.system(size: 17)).foregroundStyle(chosen ? Color.accentColor : .secondary)
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 6) {
                            Text(voice.name).font(.system(size: 14, weight: .semibold))
                            if voice.id == presentation.display.voice.current {
                                Text("Current").font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
                            }
                        }
                        Text(voice.detail).font(.system(size: 11)).foregroundStyle(.secondary)
                            .lineLimit(2).frame(height: 28, alignment: .topLeading)
                    }
                    Spacer(minLength: 0)
                }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }.buttonStyle(.plain).disabled(saving)
                .accessibilityLabel("Select \(voice.name), \(voice.detail)")
                .accessibilityValue(chosen ? "Selected" : "Not selected")
            Button { sample.toggle(voice, language: requestedLanguage, bridge: bridge) } label: {
                HStack(spacing: 6) {
                    if generating {
                        ProgressView().controlSize(.small)
                        Text("Generating…")
                        Image(systemName: "xmark")
                    } else {
                        Label(playing ? "Stop" : onlinePreview ? "Generate & Listen" : "Listen",
                              systemImage: playing ? "stop.fill" : "play.fill")
                    }
                }.font(.system(size: 12)).frame(maxWidth: .infinity)
            }.disabled(!canPreview && !generating && !playing)
                .accessibilityLabel(generating ? "Cancel \(voice.name) preview" : playing ? "Stop \(voice.name) sample" :
                    onlinePreview ? "Generate \(voice.name) preview" : "Listen to \(voice.name)")
        }
        .padding(12)
        .background(chosen ? Color.accentColor.opacity(0.07) : Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(chosen ? Color.accentColor : Color(nsColor: .separatorColor).opacity(0.5), lineWidth: chosen ? 1.5 : 0.5))
    }

    private func save() {
        saving = true
        error = nil
        sample.stop()
        bridge.data("save_voice", values: ["voice": selected, "revision": original.revision]) { result in
            saving = false
            if result.code == 0 { didSave(result.object); dismiss() }
            else { error = result.error }
        }
    }

    private func refreshSelection() {
        bridge.data("overview") { result in
            guard result.code == 0, let data = result.object["voice"] as? [String: Any] else { return }
            let wasCurrent = selected == original.current
            original = VoiceSettings(data)
            if wasCurrent { selected = original.current }
            didSave(data)
        }
    }
}
