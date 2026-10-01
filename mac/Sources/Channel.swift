import AVFoundation

/// Wall-clock time (unix seconds) of a Core Audio host time.
func wallTime(hostTime: UInt64) -> Double {
    let age = AVAudioTime.seconds(forHostTime: mach_absolute_time()) - AVAudioTime.seconds(forHostTime: hostTime)
    return Date().timeIntervalSince1970 - age
}

/// A copy of a buffer whose memory belongs to Core Audio only for the length
/// of a callback.
func copy(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
    guard let out = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength) else { return nil }
    out.frameLength = buffer.frameLength
    let from = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
    let to = UnsafeMutableAudioBufferListPointer(out.mutableAudioBufferList)
    for (source, target) in zip(from, to) {
        guard let src = source.mData, let dst = target.mData else { continue }
        memcpy(dst, src, Int(min(source.mDataByteSize, target.mDataByteSize)))
    }
    return out
}

/// One channel of a recording (the mic, or system audio): whatever PCM comes
/// in, as 20 ms Opus frames written to the spool, each stamped with when its
/// audio began. Opus at 48 kHz, its native rate; the NAS decodes it at 16 kHz.
///
/// Frames are timed end to end from where a stretch of audio began, and a
/// buffer arriving more than half a second from where that puts it starts a
/// new stretch, so a gap in the audio stays a gap on the NAS.
final class Channel {
    static let rate = 48000.0
    static let frame = 960  // 20 ms at 48 kHz
    static let frameAt16k: UInt16 = 320

    let kind: UInt8
    private let spool: Spool
    private static let mono = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: Channel.rate, channels: 1,
                                            interleaved: false)!
    private let opus: AVAudioFormat
    private var encoder: AVAudioConverter
    private var resampler: AVAudioConverter?
    private var pending: [Float] = []
    private var start: Double?      // wall time where this stretch began
    private var received = 0.0      // seconds of audio received in this stretch
    private var encoded = 0         // frames handed to the encoder in this stretch
    private var starts: [Double] = []  // start times of frames still inside the encoder
    private(set) var seconds = 0.0  // audio written since the recording began

    init(kind: UInt8, spool: Spool) throws {
        self.kind = kind
        self.spool = spool
        var description = AudioStreamBasicDescription(
            mSampleRate: Channel.rate, mFormatID: kAudioFormatOpus, mFormatFlags: 0, mBytesPerPacket: 0,
            mFramesPerPacket: UInt32(Channel.frame), mBytesPerFrame: 0, mChannelsPerFrame: 1, mBitsPerChannel: 0,
            mReserved: 0)
        guard let opus = AVAudioFormat(streamDescription: &description) else { throw Failure("no Opus format") }
        self.opus = opus
        encoder = try Channel.makeEncoder(from: Channel.mono, to: opus)
    }

    private static func makeEncoder(from mono: AVAudioFormat, to opus: AVAudioFormat) throws -> AVAudioConverter {
        guard let encoder = AVAudioConverter(from: mono, to: opus) else { throw Failure("no Opus encoder") }
        // Setting a rate the encoder doesn't offer raises an exception Swift
        // can't catch, so only one it lists.
        if encoder.applicableEncodeBitRates?.contains(NSNumber(value: 32000)) == true {
            encoder.bitRate = 32000
        }
        return encoder
    }

    /// From an audio thread: the buffer is copied before this returns.
    func append(_ buffer: AVAudioPCMBuffer, at: Double) {
        guard buffer.frameLength > 0, let owned = copy(buffer) else { return }
        spool.queue.async { self.take(owned, at: at) }
    }

    /// The end of a recording; a partial last frame is dropped.
    func finish() {
        spool.queue.async {
            self.restart(at: nil)
        }
    }

    private func restart(at: Double?) {
        pending.removeAll()
        starts.removeAll()
        start = at
        received = 0
        encoded = 0
        if let fresh = try? Channel.makeEncoder(from: Channel.mono, to: opus) { encoder = fresh }
    }

    private func take(_ buffer: AVAudioPCMBuffer, at: Double) {
        if let start, abs(at - (start + received)) > 0.5 {
            restart(at: at)
        }
        if start == nil { start = at }
        received += Double(buffer.frameLength) / buffer.format.sampleRate
        guard let samples = resample(buffer) else { return }
        pending += samples
        while pending.count >= Channel.frame {
            encode(Array(pending[..<Channel.frame]))
            pending.removeFirst(Channel.frame)
        }
    }

    private func resample(_ buffer: AVAudioPCMBuffer) -> [Float]? {
        if resampler?.inputFormat != buffer.format {
            resampler = AVAudioConverter(from: buffer.format, to: Channel.mono)
            resampler?.downmix = true
        }
        guard let resampler else { return nil }
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * Channel.rate / buffer.format.sampleRate) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: Channel.mono, frameCapacity: capacity) else { return nil }
        var given = false
        var error: NSError?
        resampler.convert(to: out, error: &error) { _, status in
            if given {
                status.pointee = .noDataNow
                return nil
            }
            given = true
            status.pointee = .haveData
            return buffer
        }
        if let error {
            log("resampling failed: \(error)")
            return nil
        }
        guard let data = out.floatChannelData else { return nil }
        return Array(UnsafeBufferPointer(start: data[0], count: Int(out.frameLength)))
    }

    private func encode(_ samples: [Float]) {
        guard let start, let input = AVAudioPCMBuffer(pcmFormat: Channel.mono, frameCapacity: AVAudioFrameCount(samples.count))
        else { return }
        input.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { input.floatChannelData![0].update(from: $0.baseAddress!, count: samples.count) }
        starts.append(start + Double(encoded * Channel.frame) / Channel.rate)
        encoded += 1

        let size = encoder.maximumOutputPacketSize > 0 ? encoder.maximumOutputPacketSize : 1500
        let out = AVAudioCompressedBuffer(format: opus, packetCapacity: 4, maximumPacketSize: size)
        var given = false
        var error: NSError?
        encoder.convert(to: out, error: &error) { _, status in
            if given {
                status.pointee = .noDataNow
                return nil
            }
            given = true
            status.pointee = .haveData
            return input
        }
        if let error {
            log("encoding failed: \(error)")
            return
        }
        guard let descriptions = out.packetDescriptions else { return }
        for i in 0..<Int(out.packetCount) {
            // The encoder can hold a frame back; packets come out in order.
            guard !starts.isEmpty else { break }
            let at = starts.removeFirst()
            let packet = descriptions[i]
            var record = Data()
            withUnsafeBytes(of: Channel.frameAt16k.littleEndian) { record.append(contentsOf: $0) }
            record.append(Data(bytes: out.data + Int(packet.mStartOffset), count: Int(packet.mDataByteSize)))
            spool.append(at: at, kind: kind, data: record)
            seconds += Double(Channel.frame) / Channel.rate
        }
    }
}

struct Failure: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}
