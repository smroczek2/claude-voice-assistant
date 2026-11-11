// One native voice-processing graph supplies the microphone and speaker reference.
// JSONL stdin: play{id,path,rate?}, rate{value}, pause, resume, stop, mute{on}, quit.
// JSONL stdout adds route{mic,output} after a device change rebuilds the graph.
import AVFoundation
import CoreAudio
import Foundation
import Darwin

private let outputLock = NSLock()
private func diagnostic(_ text: String) {
    FileHandle.standardError.write(Data(("voice-audio: " + text + "\n").utf8))
}
private func emit(_ event: [String: Any]) {
    guard var data = try? JSONSerialization.data(withJSONObject: event, options: [.sortedKeys]) else { return }
    data.append(10)
    outputLock.lock()
    FileHandle.standardOutput.write(data)
    outputLock.unlock()
}

private enum BridgeError: Error, CustomStringConvertible {
    case invalid(String)
    var description: String {
        switch self { case .invalid(let message): return message }
    }
}

private func playbackRate(_ value: Any?) throws -> Float {
    guard let number = value as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue.isFinite, (0.5...2.0).contains(number.doubleValue) else {
        throw BridgeError.invalid("Playback rate must be a finite number between 0.5 and 2.0")
    }
    return number.floatValue
}

private func command(_ line: String) throws -> [String: Any] {
    guard let data = line.data(using: .utf8),
          let value = try JSONSerialization.jsonObject(with: data) as? [String: Any],
          let type = value["type"] as? String,
          ["play", "rate", "pause", "resume", "stop", "mute", "quit"].contains(type) else {
        throw BridgeError.invalid("Expected play, rate, pause, resume, stop, mute, or quit command")
    }
    if type == "play", value["id"] == nil || !(value["path"] is String) {
        throw BridgeError.invalid("play requires id and WAV path")
    }
    if type == "mute", !(value["on"] is Bool) {
        throw BridgeError.invalid("mute requires boolean on")
    }
    if type == "play" { _ = try playbackRate(value["rate"] ?? 1.0) }
    if type == "rate" { _ = try playbackRate(value["value"]) }
    return value
}

private func convert(_ input: AVAudioPCMBuffer, using converter: AVAudioConverter,
                     to format: AVAudioFormat, final: Bool = false) throws -> AVAudioPCMBuffer {
    let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * format.sampleRate / input.format.sampleRate)) + 256
    guard let output = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else {
        throw BridgeError.invalid("Cannot allocate converted audio")
    }
    var supplied = false
    var error: NSError?
    let status = converter.convert(to: output, error: &error) { _, state in
        if supplied {
            state.pointee = final ? .endOfStream : .noDataNow
            return nil
        }
        supplied = true
        state.pointee = .haveData
        return input
    }
    if status == .error { throw error ?? BridgeError.invalid("Audio conversion failed") as NSError }
    return output
}

private func deviceName(_ selector: AudioObjectPropertySelector) -> String {
    var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                             mElement: kAudioObjectPropertyElementMain)
    var device = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &device) == noErr else { return "System default" }
    address.mSelector = kAudioObjectPropertyName
    var name: Unmanaged<CFString>?
    size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, &name) == noErr,
          let name = name else { return "System default" }
    return name.takeRetainedValue() as String
}

private final class VoiceAudio {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let timePitch = AVAudioUnitTimePitch()
    private let captureQueue = DispatchQueue(label: "voice.capture")
    private let captureSlots = DispatchSemaphore(value: 4)
    private let captureFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000,
                                              channels: 1, interleaved: true)!
    private var captureConverter: AVAudioConverter?
    private var playbackFormat: AVAudioFormat?
    private var captureMuted = false // captureQueue only
    private var activeID: Any?
    private var playbackGeneration = 0 // main queue only
    private var playbackPaused = false
    private var tapped = false

    private var reconfiguring = false // main queue only

    func start() throws {
        diagnostic("startup: get input/output nodes")
        let input = engine.inputNode
        diagnostic("before voice processing: input=\(input.outputFormat(forBus: 0)); output=\(engine.outputNode.inputFormat(forBus: 0))")
        diagnostic("startup: enable voice processing")
        try input.setVoiceProcessingEnabled(true)
        input.isVoiceProcessingBypassed = false
        input.isVoiceProcessingAGCEnabled = true
        if #available(macOS 14.0, *) {
            input.voiceProcessingOtherAudioDuckingConfiguration = .init(enableAdvancedDucking: false, duckingLevel: .min)
        }
        engine.attach(player)
        engine.attach(timePitch)
        timePitch.pitch = 0
        timePitch.rate = 1
        try buildGraph()
        // Headphones, AirPods or a new default device stop the engine and post this
        // notification. Rebuild the graph for the new device format instead of going silent.
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine,
                                               queue: .main) { [weak self] _ in self?.scheduleReconfigure() }
        diagnostic("startup: running with voice processing")
        emit(["type": "ready", "sample_rate": 16000, "channels": 1,
              "voice_processing": input.isVoiceProcessingEnabled && engine.outputNode.isVoiceProcessingEnabled,
              "mic": deviceName(kAudioHardwarePropertyDefaultInputDevice),
              "output": deviceName(kAudioHardwarePropertyDefaultOutputDevice)])
    }

    /// Connects playback and the microphone tap with the current device format, then starts the engine.
    private func buildGraph() throws {
        let input = engine.inputNode
        let output = engine.outputNode
        let inputFormat = input.outputFormat(forBus: 0)
        diagnostic("voice processing enabled: input=\(inputFormat); output=\(output.inputFormat(forBus: 0))")
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0,
              let converter = AVAudioConverter(from: inputFormat, to: captureFormat),
              let playback = AVAudioFormat(standardFormatWithSampleRate: inputFormat.sampleRate, channels: 1) else {
            throw BridgeError.invalid("No usable microphone audio format")
        }
        // Keep the processed microphone channel when VPIO exposes additional
        // channels; never downmix them into the recognizer's mono stream.
        if inputFormat.channelCount > 1 { converter.channelMap = [0] }
        captureQueue.sync { captureConverter = converter }
        playbackFormat = playback
        diagnostic("startup: connect player and matched voice-processing output")
        engine.connect(player, to: timePitch, format: playback)
        engine.connect(timePitch, to: engine.mainMixerNode, format: playback)
        // AVAudioIONode requires the processed input's output format and the
        // output node's input format to match while voice processing is enabled.
        // The implicit mixer connection can otherwise retain a stereo format.
        engine.connect(engine.mainMixerNode, to: output, format: inputFormat)
        diagnostic("connected graph: input=\(input.outputFormat(forBus: 0)); output=\(output.inputFormat(forBus: 0)); mixer=\(engine.mainMixerNode.outputFormat(forBus: 0))")
        installCaptureTap(format: inputFormat)
        diagnostic("startup: prepare engine")
        engine.prepare()
        diagnostic("startup: start engine")
        try engine.start()
    }

    private func installCaptureTap(format: AVAudioFormat) {
        diagnostic("startup: install processed microphone tap")
        engine.inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let self = self, self.captureSlots.wait(timeout: .now()) == .success else { return }
            // Bounded copy only on the tap; resampling, JSON, and pipe writes run
            // on the capture queue. Never let a slow Python reader block audio.
            guard let copy = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength) else {
                self.captureSlots.signal(); return
            }
            copy.frameLength = buffer.frameLength
            let source = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
            let destination = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
            for index in source.indices {
                if let src = source[index].mData, let dst = destination[index].mData {
                    memcpy(dst, src, Int(source[index].mDataByteSize))
                }
            }
            self.captureQueue.async {
                defer { self.captureSlots.signal() }
                // A buffer from the previous device can arrive after a rebuild; drop it.
                guard !self.captureMuted, let converter = self.captureConverter,
                      converter.inputFormat == copy.format else { return }
                do {
                    let pcm = try convert(copy, using: converter, to: self.captureFormat)
                    guard pcm.frameLength > 0, let samples = pcm.int16ChannelData?[0] else { return }
                    let data = Data(bytes: samples, count: Int(pcm.frameLength) * 2)
                    emit(["type": "audio", "pcm": data.base64EncodedString()])
                } catch { emit(["type": "error", "error": "Capture conversion: \(error)"]) }
            }
        }
        tapped = true
    }

    /// Several change notifications arrive while a device connects; rebuild once after they settle.
    private func scheduleReconfigure() {
        guard !reconfiguring else { return }
        reconfiguring = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.reconfigure(attempt: 1) }
    }

    private func reconfigure(attempt: Int) {
        diagnostic("audio route changed: rebuild graph (attempt \(attempt))")
        stop() // An interrupted reply reports cancelled, so the daemon does not wait on it.
        if tapped { engine.inputNode.removeTap(onBus: 0); tapped = false }
        engine.stop()
        engine.disconnectNodeOutput(player)
        engine.disconnectNodeOutput(timePitch)
        engine.disconnectNodeOutput(engine.mainMixerNode)
        do {
            try buildGraph()
            reconfiguring = false
            let mic = deviceName(kAudioHardwarePropertyDefaultInputDevice)
            let out = deviceName(kAudioHardwarePropertyDefaultOutputDevice)
            diagnostic("audio route changed: running on mic=\(mic) output=\(out)")
            emit(["type": "route", "mic": mic, "output": out])
        } catch {
            guard attempt < 5 else {
                diagnostic("audio route change failed: \(error as NSError)")
                emit(["type": "fatal", "error": "Audio device change failed: \(error)"])
                Darwin.exit(1)
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in self?.reconfigure(attempt: attempt + 1) }
        }
    }

    func handle(_ value: [String: Any]) {
        do {
            switch value["type"] as? String {
            case "play": try play(value)
            case "rate":
                timePitch.rate = try playbackRate(value["value"])
                emit(["type": "rate", "value": timePitch.rate])
            case "pause": playbackPaused = true; player.pause(); emit(["type": "paused"])
            case "resume":
                playbackPaused = false
                if activeID != nil { player.play() }
                emit(["type": "resumed"])
            case "stop": stop(); emit(["type": "stopped"])
            case "mute":
                let muted = value["on"] as! Bool
                // Muting capture leaves the shared engine/AEC reference running.
                captureQueue.async {
                    self.captureMuted = muted
                    self.captureConverter?.reset()
                    emit(["type": "muted", "on": muted])
                }
            case "quit": shutdown(); Darwin.exit(0)
            default: throw BridgeError.invalid("Unknown command")
            }
        } catch {
            var event: [String: Any] = ["type": "error", "error": String(describing: error)]
            if let id = value["id"] { event["id"] = id }
            emit(event)
        }
    }

    private func play(_ value: [String: Any]) throws {
        let rate = try playbackRate(value["rate"] ?? 1.0)
        guard let path = value["path"] as? String, let id = value["id"], let format = playbackFormat else {
            throw BridgeError.invalid("Audio engine is not ready")
        }
        let file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
        guard file.length > 0, file.length <= AVAudioFramePosition(UInt32.max),
              let source = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length)),
              let converter = AVAudioConverter(from: file.processingFormat, to: format) else {
            throw BridgeError.invalid("Invalid or unsupported playback file")
        }
        try file.read(into: source)
        let buffer = try convert(source, using: converter, to: format, final: true)
        stop(clearPause: false)
        // A stopped engine plays nothing and never reports the reply as played, so the
        // daemon would show "speaking" in silence. Restart it, or fail this reply clearly.
        if !engine.isRunning {
            diagnostic("play: engine was stopped; restarting")
            do { try engine.start() } catch {
                throw BridgeError.invalid("Speaker output stopped and did not restart: \(error)")
            }
        }
        timePitch.rate = rate
        activeID = id
        let generation = playbackGeneration
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self, self.playbackGeneration == generation, self.activeID != nil else { return }
                self.activeID = nil
                emit(["type": "played", "id": id, "cancelled": false])
            }
        }
        if !playbackPaused { player.play() }
    }

    private func stop(clearPause: Bool = true) {
        playbackGeneration += 1
        if clearPause { playbackPaused = false }
        player.stop()
        timePitch.reset() // Discard the effect's buffered tail on stop, never on pause.
        if let id = activeID {
            activeID = nil
            emit(["type": "played", "id": id, "cancelled": true])
        }
    }

    func shutdown() {
        stop()
        if tapped { engine.inputNode.removeTap(onBus: 0); tapped = false }
        engine.stop()
        emit(["type": "bye"])
    }
}

if CommandLine.arguments.contains("--self-test") {
    do {
        _ = try command("{\"type\":\"play\",\"id\":7,\"path\":\"/tmp/test.wav\"}")
        for rate in [0.5, 1.0, 1.4, 2.0] {
            _ = try command("{\"type\":\"play\",\"id\":7,\"path\":\"/tmp/test.wav\",\"rate\":\(rate)}")
            _ = try command("{\"type\":\"rate\",\"value\":\(rate)}")
        }
        for invalid in ["0.49", "2.01", "true", "null", "\"fast\"", "1e999", "NaN"] {
            for line in ["{\"type\":\"rate\",\"value\":\(invalid)}",
                         "{\"type\":\"play\",\"id\":7,\"path\":\"/tmp/test.wav\",\"rate\":\(invalid)}"] {
                var rejected = false
                do { _ = try command(line) } catch { rejected = true }
                guard rejected else { throw BridgeError.invalid("Invalid rate accepted: \(invalid)") }
            }
        }
        for invalid in [Double.nan, Double.infinity, -Double.infinity] {
            var rejected = false
            do { _ = try playbackRate(invalid) } catch { rejected = true }
            guard rejected else { throw BridgeError.invalid("Nonfinite rate accepted") }
        }
        var missingRateRejected = false
        do { _ = try command("{\"type\":\"rate\"}") } catch { missingRateRejected = true }
        guard missingRateRejected else { throw BridgeError.invalid("Missing rate accepted") }
        _ = try command("{\"type\":\"mute\",\"on\":true}")
        _ = try command("{\"type\":\"stop\"}")
        _ = try command("{\"type\":\"pause\"}")
        _ = try command("{\"type\":\"resume\"}")
        _ = try command("{\"type\":\"quit\"}")
        do { _ = try command("{\"type\":\"play\"}"); throw BridgeError.invalid("Invalid play accepted") }
        catch BridgeError.invalid(let reason) where reason == "play requires id and WAV path" {}
        let sourceFormat = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 1)!
        let targetFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
        let source = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: 4800)!
        source.frameLength = 4800
        for index in 0..<4800 { source.floatChannelData![0][index] = Float(sin(Double(index) * 0.04) * 0.1) }
        let result = try convert(source, using: AVAudioConverter(from: sourceFormat, to: targetFormat)!, to: targetFormat, final: true)
        guard result.frameLength > 1400 && result.frameLength < 1800 else { throw BridgeError.invalid("Wrong resampled frame count") }
        let pcm = Data(bytes: result.int16ChannelData![0], count: Int(result.frameLength) * 2)
        guard Data(base64Encoded: pcm.base64EncodedString()) == pcm else { throw BridgeError.invalid("PCM protocol round trip failed") }
        emit(["type": "self_test", "ok": true, "frames": result.frameLength, "rate_validation": true, "hardware_opened": false])
        Darwin.exit(0)
    } catch { emit(["type": "self_test", "ok": false, "error": String(describing: error)]); Darwin.exit(1) }
}

private let bridge = VoiceAudio()
// EOF means the owning Python process has closed or died. Exit from this reader
// thread even if CoreAudio is awaiting a permission response on the main queue.
Thread.detachNewThread {
    while let line = readLine() {
        do {
            let value = try command(line)
            DispatchQueue.main.async { bridge.handle(value) }
        } catch { emit(["type": "error", "error": String(describing: error)]) }
    }
    DispatchQueue.main.async { bridge.shutdown(); Darwin.exit(0) }
    // A permission prompt can stall the main queue. EOF still bounds the
    // helper's lifetime; process teardown releases the device in that case.
    usleep(250_000)
    Darwin._exit(0)
}
DispatchQueue.main.async {
    do { try bridge.start() }
    catch {
        diagnostic("startup failed: \(error as NSError)")
        emit(["type": "fatal", "error": String(describing: error)])
        Darwin.exit(1)
    }
}
dispatchMain()
