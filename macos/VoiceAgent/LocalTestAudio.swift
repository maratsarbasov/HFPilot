import AVFoundation
import Foundation
import Darwin

// The app executable also runs as a private PCM child of the backend. Keeping
// capture in this bundle gives microphone permission the same HFPilot identity.
enum LocalTestAudio {
    private static func event(_ value: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: value) {
            try? FileHandle.standardError.write(contentsOf: data + Data([10]))
        }
    }

    private static func fail(_ message: String) -> Never {
        event(["event": "error", "message": message])
        exit(1)
    }

    static func run() {
        guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            fail("Allow HFPilot to use the microphone in System Settings → Privacy & Security → Microphone.")
        }
        signal(SIGPIPE, SIG_IGN)
        // Capture must never block an audio callback when the backend stalls.
        let flags = fcntl(STDOUT_FILENO, F_GETFL)
        guard flags >= 0, fcntl(STDOUT_FILENO, F_SETFL, flags | O_NONBLOCK) == 0 else {
            fail("The microphone audio pipe could not be opened.")
        }
        let engine = AVAudioEngine()
        let input = engine.inputNode
        do { try input.setVoiceProcessingEnabled(true) }
        catch { fail("Voice processing could not start: \(error.localizedDescription)") }
        let captureFormat = input.outputFormat(forBus: 0)
        let outputFormat = engine.outputNode.inputFormat(forBus: 0)
        guard captureFormat.sampleRate > 0, captureFormat.channelCount > 0,
              outputFormat.sampleRate > 0, outputFormat.channelCount > 0,
              let devicePlayback = AVAudioFormat(standardFormatWithSampleRate: captureFormat.sampleRate,
                                                  channels: outputFormat.channelCount),
              let pcm = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 48000,
                                      channels: 1, interleaved: false),
              let playback = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 1),
              let converter = AVAudioConverter(from: captureFormat, to: pcm) else {
            fail("Select an available default microphone and output device in Sound settings.")
        }
        // Voice processing can expose additional reference channels. Channel 0
        // is the processed microphone, not the speaker reference.
        converter.channelMap = [0]
        var pending = Data()
        input.installTap(onBus: 0, bufferSize: 960, format: captureFormat) { buffer, _ in
            let capacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * 48000 / captureFormat.sampleRate) + 64)
            guard let converted = AVAudioPCMBuffer(pcmFormat: pcm, frameCapacity: capacity) else { return }
            var supplied = false
            var error: NSError?
            let status = converter.convert(to: converted, error: &error) { _, inputStatus in
                if supplied { inputStatus.pointee = .noDataNow; return nil }
                supplied = true
                inputStatus.pointee = .haveData
                return buffer
            }
            if status == .error {
                DispatchQueue.main.async { fail("Microphone conversion failed: \(error?.localizedDescription ?? "Unknown error")") }
                return
            }
            if let samples = converted.int16ChannelData?[0] {
                pending.append(Data(bytes: samples, count: Int(converted.frameLength) * 2))
            }
            while !pending.isEmpty {
                // PIPE_BUF is only 512 on macOS. Keep writes atomic so a full
                // pipe cannot leave half of an Int16 sample on the wire.
                let size = min(Int(PIPE_BUF), pending.count)
                let count = pending.withUnsafeBytes { Darwin.write(STDOUT_FILENO, $0.baseAddress!, size) }
                if count > 0 { pending.removeFirst(count); continue }
                if count < 0 && errno != EAGAIN && errno != EINTR {
                    DispatchQueue.main.async { fail("The microphone audio pipe closed.") }
                }
                // Bound delayed capture to 100 ms and discard whole PCM frames.
                while pending.count > 9600 { pending.removeFirst(1920) }
                break
            }
        }
        let player = AVAudioPlayerNode()
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: playback)
        // VPIO requires the client input/output sample rates to match. The
        // mixer's implicit output connection can retain 44.1 kHz even when the
        // processed microphone is 48 kHz, causing initialization error -10875.
        // Convert our fixed 48 kHz playback in the mixer and explicitly match
        // the microphone rate at the device boundary, retaining output channels.
        engine.connect(engine.mainMixerNode, to: engine.outputNode, format: devicePlayback)
        do { try engine.start() }
        catch { fail("Mac audio could not start: \(error.localizedDescription)") }
        player.play()
        // A route change invalidates converters. End this test with an actionable
        // error rather than silently retaining the previous device or format.
        let observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange,
            object: engine, queue: .main) { _ in
                fail("Audio devices changed. Start a new test to use the current default devices.")
            }
        event(["event": "ready", "capture_rate": captureFormat.sampleRate,
               "capture_channels": captureFormat.channelCount,
               "playback_rate": devicePlayback.sampleRate,
               "playback_channels": devicePlayback.channelCount])
        var lastInput = Date()
        let watchdog = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            if Date().timeIntervalSince(lastInput) > 5 {
                fail("The test call audio connection stopped responding.")
            }
        }
        DispatchQueue.global(qos: .userInteractive).async {
            let slots = DispatchSemaphore(value: 5)
            var data = Data()
            while true {
                do {
                    guard let part = try FileHandle.standardInput.read(upToCount: 1920 - data.count), !part.isEmpty else {
                        DispatchQueue.main.async {
                            watchdog.invalidate()
                            NotificationCenter.default.removeObserver(observer)
                            player.stop()
                            engine.stop()
                            exit(0)
                        }
                        return
                    }
                    data.append(part)
                    DispatchQueue.main.async { lastInput = Date() }
                } catch { fail("The speaker audio pipe closed.") }
                guard data.count == 1920 else { continue }
                guard slots.wait(timeout: .now() + 2) == .success else {
                    fail("The default output device stopped playing audio.")
                }
                guard let buffer = AVAudioPCMBuffer(pcmFormat: playback, frameCapacity: 960),
                      let samples = buffer.floatChannelData?[0] else { fail("Could not allocate speaker audio.") }
                buffer.frameLength = 960
                data.withUnsafeBytes { raw in
                    for i in 0..<960 {
                        samples[i] = Float(Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))) / 32768
                    }
                }
                player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { _ in slots.signal() }
                data.removeAll(keepingCapacity: true)
            }
        }
        RunLoop.main.run()
    }
}
