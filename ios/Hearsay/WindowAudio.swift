import AVFoundation

/// A paused window decoded for listening and cropping on the phone.
///
/// Frames are timed as hearsay/capture.py times them: a live frame ends about
/// when it arrived, a stored packet's frames end about half a second after
/// its whole-second pendant time, and frames run end to end until one lands
/// more than a second off. Silence between runs is skipped, so the player
/// plays only audio; `wallTime` maps a position back to the clock. Lost
/// frames are skipped rather than concealed: this is for listening, and the
/// NAS still decodes the kept records itself.
///
/// The decoded audio is a WAV in the app's temporary directory, which is
/// never backed up; it's deleted when the window is closed.
final class WindowAudio {
    struct Run {
        let wallStart: Double
        let offset: Double  // seconds into the decoded audio
        let duration: Double
    }

    static let sampleRate = 16000.0
    // Codec byte -> samples per Opus frame, as in hearsay/capture.py.
    static let frameSamples: [UInt8: Int] = [20: 160, 21: 320]
    private static let reanchor = 1.0
    // Above this, a cropped upload is split; the receiver reads bodies whole.
    private static let maxUploadBytes = 4_000_000

    let wav: URL
    private(set) var runs: [Run] = []
    private let records: [Spool.Record]

    var duration: Double { runs.last.map { $0.offset + $0.duration } ?? 0 }

    init(dir: URL) throws {
        let names = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".upload") }.sorted()
        records = try names.flatMap { try Spool.records(in: dir.appendingPathComponent($0)) }
        wav = FileManager.default.temporaryDirectory.appendingPathComponent("window-\(UUID().uuidString).wav")
        try decode()
    }

    func close() {
        try? FileManager.default.removeItem(at: wav)
    }

    /// Left behind by a crash while a window was open.
    static func removeLeftovers() {
        let tmp = FileManager.default.temporaryDirectory
        for name in (try? FileManager.default.contentsOfDirectory(atPath: tmp.path)) ?? [] where name.hasPrefix("window-") {
            try? FileManager.default.removeItem(at: tmp.appendingPathComponent(name))
        }
    }

    func wallTime(_ position: Double) -> Date {
        let run = runs.last { $0.offset <= position } ?? runs.first
        return Date(timeIntervalSince1970: (run?.wallStart ?? 0) + position - (run?.offset ?? 0))
    }

    /// The records from `from` to `to` (positions in the decoded audio), as
    /// uploads. Each starts with a CONNECTED record carrying the codec, as
    /// the NAS needs to decode it and to restart its count of the pendant's
    /// packet index.
    func cropped(from: Double, to: Double) -> [Data] {
        let start = wallTime(from).timeIntervalSince1970, end = wallTime(to).timeIntervalSince1970
        var uploads: [Data] = []
        var current = Data()
        var codec: UInt8?
        for record in records {
            switch record.kind {
            case Spool.connected:
                codec = record.data.first
                guard record.at >= start && record.at <= end else { continue }
            case Spool.audio:
                // The frame ends about when it arrived.
                let length = codec.flatMap { Self.frameSamples[$0] }.map { Double($0) / Self.sampleRate } ?? 0
                guard record.at >= start && record.at - length <= end else { continue }
            case Spool.stored:
                guard let packet = Self.storedFrames(record) else { continue }
                guard packet.end >= start && packet.end - packet.duration <= end else { continue }
            default:
                guard record.at >= start && record.at <= end else { continue }
            }
            let encoded = record.encoded
            if !current.isEmpty && current.count + encoded.count > Self.maxUploadBytes {
                uploads.append(current)
                current = Data()
            }
            if current.isEmpty, let codec {
                current = Spool.Record(at: record.at, kind: Spool.connected, data: Data([codec])).encoded
            }
            current.append(encoded)
        }
        if !current.isEmpty { uploads.append(current) }
        return uploads
    }

    // MARK: Decoding

    private struct Frame {
        let end: Double
        let codec: UInt8
        let bytes: Data
    }

    private func decode() throws {
        var frames: [Frame] = []
        var codec: UInt8?
        for record in records {
            switch record.kind {
            case Spool.connected:
                codec = record.data.first
            case Spool.audio:
                // [index u16][sub u8][Opus]; at the pendant's MTU a frame
                // always fits one notification (sub 0).
                guard let codec, record.data.count > 3, record.data[record.data.startIndex + 2] == 0 else { continue }
                frames.append(Frame(end: record.at, codec: codec, bytes: Data(record.data.dropFirst(3))))
            case Spool.stored:
                guard let packet = Self.storedFrames(record) else { continue }
                let length = packet.duration / Double(packet.frames.count)
                for (i, bytes) in packet.frames.enumerated() {
                    let end = packet.end - Double(packet.frames.count - 1 - i) * length
                    frames.append(Frame(end: end, codec: packet.codec, bytes: bytes))
                }
            default:
                break
            }
        }
        frames.sort { $0.end < $1.end }

        let pcm = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Self.sampleRate, channels: 1, interleaved: true)!
        let file = try AVAudioFile(forWriting: wav, settings: pcm.settings, commonFormat: .pcmFormatInt16, interleaved: true)
        var decoder: Decoder?
        var runStart = 0.0, runCodec: UInt8 = 0, expectedEnd = 0.0, written = 0.0, runWritten = 0.0
        for frame in frames {
            guard let samples = Self.frameSamples[frame.codec] else { continue }
            let length = Double(samples) / Self.sampleRate
            if decoder == nil || frame.codec != runCodec || abs(frame.end - (expectedEnd + length)) > Self.reanchor {
                if decoder != nil { runs.append(Run(wallStart: runStart, offset: written - runWritten, duration: runWritten)) }
                decoder = try Decoder(codec: frame.codec, samples: samples, pcm: pcm)
                runStart = frame.end - length
                runCodec = frame.codec
                expectedEnd = runStart
                runWritten = 0
            }
            expectedEnd += length
            guard let buffer = decoder?.decode(frame.bytes), buffer.frameLength > 0 else { continue }
            try file.write(from: buffer)
            written += Double(buffer.frameLength) / Self.sampleRate
            runWritten += Double(buffer.frameLength) / Self.sampleRate
        }
        if decoder != nil { runs.append(Run(wallStart: runStart, offset: written - runWritten, duration: runWritten)) }
    }

    /// A stored record's frames and when they end; layout in Pendant and
    /// hearsay/capture.py (stored_frames).
    private static func storedFrames(_ record: Spool.Record) -> (codec: UInt8, end: Double, duration: Double, frames: [Data])? {
        let data = record.data
        guard data.count == 17 + Pendant.storedPacketBytes, let samples = frameSamples[data[data.startIndex]] else { return nil }
        let stampStart = data.startIndex + 17
        let stamp = data[stampStart..<stampStart + 4].reduce(0) { $0 << 8 | UInt32($1) }
        let audio = data[(stampStart + 4)...]
        var frames: [Data] = []
        var offset = audio.startIndex
        while offset < audio.endIndex - 1 {
            let size = Int(audio[offset])
            if size == 0 {
                offset += 1
                continue
            }
            if offset + 1 + size >= audio.endIndex { break }
            frames.append(Data(audio[(offset + 1)..<(offset + 1 + size)]))
            offset += 1 + size
        }
        guard !frames.isEmpty else { return nil }
        let duration = Double(frames.count * samples) / sampleRate
        return (data[data.startIndex], Double(stamp) + 0.5, duration, frames)
    }

    /// iOS's own Opus decoder, one per run so each starts clean.
    private struct Decoder {
        let opus: AVAudioFormat
        let pcm: AVAudioFormat
        let converter: AVAudioConverter
        let samples: Int

        init(codec: UInt8, samples: Int, pcm: AVAudioFormat) throws {
            var description = AudioStreamBasicDescription(
                mSampleRate: WindowAudio.sampleRate, mFormatID: kAudioFormatOpus, mFormatFlags: 0, mBytesPerPacket: 0,
                mFramesPerPacket: UInt32(samples), mBytesPerFrame: 0, mChannelsPerFrame: 1, mBitsPerChannel: 0, mReserved: 0)
            guard let opus = AVAudioFormat(streamDescription: &description),
                  let converter = AVAudioConverter(from: opus, to: pcm) else {
                throw CocoaError(.featureUnsupported)
            }
            self.opus = opus
            self.pcm = pcm
            self.converter = converter
            self.samples = samples
        }

        func decode(_ bytes: Data) -> AVAudioPCMBuffer? {
            guard !bytes.isEmpty else { return nil }
            let input = AVAudioCompressedBuffer(format: opus, packetCapacity: 1, maximumPacketSize: bytes.count)
            bytes.withUnsafeBytes { input.data.copyMemory(from: $0.baseAddress!, byteCount: bytes.count) }
            input.byteLength = UInt32(bytes.count)
            input.packetCount = 1
            input.packetDescriptions?[0] = AudioStreamPacketDescription(
                mStartOffset: 0, mVariableFramesInPacket: 0, mDataByteSize: UInt32(bytes.count))
            guard let output = AVAudioPCMBuffer(pcmFormat: pcm, frameCapacity: AVAudioFrameCount(samples)) else { return nil }
            var given = false
            var error: NSError?
            let status = converter.convert(to: output, error: &error) { _, state in
                if given {
                    state.pointee = .noDataNow
                    return nil
                }
                given = true
                state.pointee = .haveData
                return input
            }
            return status == .error ? nil : output
        }
    }
}
