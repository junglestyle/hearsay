import AVFoundation

/// The default input (the built-in mic, or AirPods when they're the input).
/// macOS asks the operator once for permission (NSMicrophoneUsageDescription).
final class Mic {
    private var engine: AVAudioEngine?

    func start(into channel: Channel) throws {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else { throw Failure("no microphone; allow microphone access?") }
        input.installTap(onBus: 0, bufferSize: 4800, format: format) { buffer, when in
            let at = when.isHostTimeValid
                ? wallTime(hostTime: when.hostTime)
                : Date().timeIntervalSince1970 - Double(buffer.frameLength) / format.sampleRate
            channel.append(buffer, at: at)
        }
        try engine.start()
        self.engine = engine
    }

    func stop() {
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
    }
}
