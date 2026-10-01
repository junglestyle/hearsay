import AVFoundation
import CoreAudio

private func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

private func value<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ initial: T) -> T? {
    var where_ = address(selector)
    var result = initial
    var size = UInt32(MemoryLayout<T>.size)
    let status = withUnsafeMutablePointer(to: &result) { AudioObjectGetPropertyData(object, &where_, 0, nil, &size, $0) }
    return status == noErr ? result : nil
}

private func string(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var where_ = address(selector)
    var result: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let status = withUnsafeMutablePointer(to: &result) { AudioObjectGetPropertyData(object, &where_, 0, nil, &size, $0) }
    guard status == noErr, let result else { return nil }
    return result.takeRetainedValue() as String
}

/// Core Audio's objects for running processes that use audio.
private func processes() -> [AudioObjectID] {
    var where_ = address(kAudioHardwarePropertyProcessObjectList)
    let system = AudioObjectID(kAudioObjectSystemObject)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(system, &where_, 0, nil, &size) == noErr, size > 0 else { return [] }
    var list = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(system, &where_, 0, nil, &size, &list) == noErr else { return [] }
    return list
}

/// Zoom's audio processes (the app and its helpers).
func zoomProcesses() -> [AudioObjectID] {
    processes().filter { (string($0, kAudioProcessPropertyBundleID) ?? "").hasPrefix("us.zoom.") }
}

/// A Zoom call is on while Zoom has a microphone open: it does for the whole
/// meeting, muted or not, and not otherwise.
func zoomCallActive() -> Bool {
    zoomProcesses().contains { (value($0, kAudioProcessPropertyIsRunningInput, UInt32(0)) ?? 0) != 0 }
}

/// System audio through a Core Audio process tap: of the given processes
/// only, or of everything when there are none. macOS asks the operator once
/// for permission (NSAudioCaptureUsageDescription).
final class SystemAudio {
    private var tap = AudioObjectID(kAudioObjectUnknown)
    private var aggregate = AudioObjectID(kAudioObjectUnknown)
    private var proc: AudioDeviceIOProcID?
    private let queue = DispatchQueue(label: "hearsay.system-audio", qos: .userInitiated)

    func start(processes: [AudioObjectID], into channel: Channel) throws {
        let description = processes.isEmpty
            ? CATapDescription(stereoGlobalTapButExcludeProcesses: [])
            : CATapDescription(stereoMixdownOfProcesses: processes.map { NSNumber(value: $0) })
        description.uuid = UUID()
        description.muteBehavior = .unmuted
        var status = AudioHardwareCreateProcessTap(description, &tap)
        guard status == noErr else { throw Failure("can't create the audio tap (\(status)); allow system audio recording?") }

        guard var stream = value(tap, kAudioTapPropertyFormat, AudioStreamBasicDescription()),
              let format = AVAudioFormat(streamDescription: &stream) else {
            stop()
            throw Failure("can't read the audio tap's format")
        }
        guard let output = value(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultSystemOutputDevice,
                                 AudioObjectID(kAudioObjectUnknown)),
              let outputUID = string(output, kAudioDevicePropertyDeviceUID) else {
            stop()
            throw Failure("no output device")
        }
        let settings: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Hearsay tap",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                               kAudioSubTapUIDKey: description.uuid.uuidString]],
        ]
        status = AudioHardwareCreateAggregateDevice(settings as CFDictionary, &aggregate)
        guard status == noErr else {
            stop()
            throw Failure("can't create the tap's aggregate device (\(status))")
        }
        status = AudioDeviceCreateIOProcIDWithBlock(&proc, aggregate, queue) { _, input, inputTime, _, _ in
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: input, deallocator: nil) else { return }
            channel.append(buffer, at: wallTime(hostTime: inputTime.pointee.mHostTime))
        }
        guard status == noErr else {
            stop()
            throw Failure("can't read from the tap (\(status))")
        }
        status = AudioDeviceStart(aggregate, proc)
        guard status == noErr else {
            stop()
            throw Failure("can't start the tap (\(status))")
        }
    }

    func stop() {
        if aggregate != kAudioObjectUnknown {
            if let proc {
                AudioDeviceStop(aggregate, proc)
                AudioDeviceDestroyIOProcID(aggregate, proc)
            }
            AudioHardwareDestroyAggregateDevice(aggregate)
        }
        if tap != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tap)
        }
        proc = nil
        aggregate = AudioObjectID(kAudioObjectUnknown)
        tap = AudioObjectID(kAudioObjectUnknown)
    }
}
