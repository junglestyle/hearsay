import CoreBluetooth
import Foundation

/// The Omi pendant over BLE: find it, stay connected, and spool everything it
/// sends. Nothing is decoded here; the NAS does that from the spooled bytes.
///
/// UUIDs and formats are from Omi's firmware (BasedHardware/omi, MIT); see
/// docs/pendant-ble.md.
///
/// Runs in the background (UIBackgroundModes bluetooth-central). The pending
/// connect never times out, so iOS completes it whenever the pendant comes
/// back in range, and with state restoration iOS relaunches the app for it
/// even after it was terminated.
final class Pendant: NSObject, ObservableObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    static let audioService = CBUUID(string: "19B10000-E8F2-537E-4F6C-D104768A1214")
    static let audioData = CBUUID(string: "19B10001-E8F2-537E-4F6C-D104768A1214")
    static let audioCodec = CBUUID(string: "19B10002-E8F2-537E-4F6C-D104768A1214")
    static let buttonService = CBUUID(string: "23BA7924-0000-1000-7450-346EAC492E92")
    static let buttonEvent = CBUUID(string: "23BA7925-0000-1000-7450-346EAC492E92")
    static let timeService = CBUUID(string: "19B10030-E8F2-537E-4F6C-D104768A1214")
    static let timeWrite = CBUUID(string: "19B10031-E8F2-537E-4F6C-D104768A1214")
    static let settingsService = CBUUID(string: "19B10010-E8F2-537E-4F6C-D104768A1214")
    static let micGain = CBUUID(string: "19B10012-E8F2-537E-4F6C-D104768A1214")
    static let storageService = CBUUID(string: "30295780-4301-EABD-2904-2849ADFEAE43")
    static let storageControl = CBUUID(string: "30295781-4301-EABD-2904-2849ADFEAE43")
    /// A stored packet: [pendant time u32 BE][440 bytes of [len u8][Opus]...].
    static let storedPacketBytes = 444
    private static let rememberedKey = "pendantID"
    private static let mutedKey = "muted"
    private static let gainBeforeMuteKey = "micGainBeforeMute"
    private static let defaultGain: UInt8 = 6
    private static let awaySinceKey = "awaySince"

    @Published private(set) var state = "starting"
    @Published private(set) var lastMinute = ""
    /// What the operator asked for; applied whenever the pendant is connected.
    @Published private(set) var muted = UserDefaults.standard.bool(forKey: Pendant.mutedKey)
    @Published private(set) var stored = ""

    /// Decides where downloaded audio goes; set by Pause.
    weak var pause: Pause?

    /// Where what the pendant sends goes; nil drops it (paused).
    private var spool: Spool?
    private var central: CBCentralManager!
    private var peripheral: CBPeripheral?
    private var codec: Data?
    private var gain: CBCharacteristic?
    private var storage: CBCharacteristic?
    private var storageNotifying = false
    private var downloading = false
    // The transfer in progress: the next packet's sequence number on the
    // pendant, the bytes of a packet not yet complete, and progress.
    private var storedSeq: UInt64 = 0
    private var storedBuffer = Data()
    private var storedTotal: UInt32 = 0
    private var storedReceived = 0
    private var storedThisMinute = 0

    // For the once-a-minute summary.
    private var packets = 0
    private var lost = 0
    private var taps = 0
    private var lastIndex: UInt16?

    override init() {
        super.init()
        central = CBCentralManager(delegate: self, queue: .main,
                                   options: [CBCentralManagerOptionRestoreIdentifierKey: "pendant"])
    }

    /// Send from now on to `spool`, or drop if nil. Each destination's audio
    /// starts with a CONNECTED record, as after a connect: the NAS needs the
    /// codec to decode it, and the record restarts its count of the pendant's
    /// packet index, which jumps over whatever went elsewhere.
    func route(to spool: Spool?) {
        self.spool = spool
        if let codec { spool?.append(kind: Spool.connected, data: codec) }
    }

    /// Mute in hardware: mic gain 0, saved on the pendant, so it holds out of
    /// range and across app crashes until unmuted. Unmuting restores the
    /// level from before.
    func setMuted(_ on: Bool) {
        muted = on
        UserDefaults.standard.set(on, forKey: Self.mutedKey)
        log.info("\(on ? "muting" : "unmuting")")
        // Reconciled against the pendant's level once it's read.
        if let gain, let peripheral, peripheral.state == .connected {
            peripheral.readValue(for: gain)
        }
    }

    func report() {
        lastMinute = "\(packets) audio packets, \(lost) lost, \(taps) button events, \(storedThisMinute) stored packets"
        log.info("\(self.state): \(self.lastMinute) in the last minute")
        packets = 0
        lost = 0
        taps = 0
        storedThisMinute = 0
    }

    /// Drop the remembered pendant and pair with the next one found.
    func forget() {
        UserDefaults.standard.removeObject(forKey: Self.rememberedKey)
        if let peripheral { central.cancelPeripheralConnection(peripheral) }
        peripheral = nil
        scan()
    }

    func centralManager(_ central: CBCentralManager, willRestoreState dict: [String: Any]) {
        guard let restored = (dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral])?.first else { return }
        log.info("restored by iOS, pendant \(restored.state.rawValue)")
        peripheral = restored
        restored.delegate = self
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        switch central.state {
        case .poweredOn:
            if let peripheral, peripheral.state == .connected {
                // Relaunched by iOS mid-connection: set it up again, which
                // also writes a fresh CONNECTED record.
                setUp(peripheral)
            } else if let peripheral {
                markAway()
                connect(peripheral)
            } else if let text = UserDefaults.standard.string(forKey: Self.rememberedKey),
                      let id = UUID(uuidString: text),
                      let known = central.retrievePeripherals(withIdentifiers: [id]).first {
                // One pendant: the first one found is remembered, and only it is used.
                markAway()
                connect(known)
            } else {
                scan()
            }
        case .unauthorized:
            state = "Bluetooth access denied: allow it in Settings > Hearsay"
        default:
            state = "Bluetooth is off"
        }
    }

    private func scan() {
        guard central.state == .poweredOn else { return }
        state = "looking for the pendant"
        central.scanForPeripherals(withServices: [Self.audioService])
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi: NSNumber) {
        central.stopScan()
        UserDefaults.standard.set(peripheral.identifier.uuidString, forKey: Self.rememberedKey)
        log.info("found pendant \(peripheral.name ?? "?") \(peripheral.identifier)")
        connect(peripheral)
    }

    private func connect(_ found: CBPeripheral) {
        peripheral = found
        found.delegate = self
        state = "waiting for the pendant"
        central.connect(found)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        setUp(peripheral)
    }

    private func setUp(_ peripheral: CBPeripheral) {
        state = "connected"
        log.info("connected")
        lastIndex = nil
        codec = nil
        gain = nil
        storage = nil
        storageNotifying = false
        endDownload()
        peripheral.discoverServices([Self.audioService, Self.buttonService, Self.timeService, Self.settingsService,
                                      Self.storageService])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        log.error("connect failed: \(error.map { "\($0)" } ?? "unknown"); retrying")
        central.connect(peripheral)
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral,
                        error: Error?) {
        log.info("disconnected: \(error.map { "\($0)" } ?? "no error")")
        endDownload()
        // Forgotten on purpose: don't reconnect to it.
        guard peripheral == self.peripheral else { return }
        markAway()
        state = "waiting for the pendant"
        central.connect(peripheral)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        for service in peripheral.services ?? [] {
            peripheral.discoverCharacteristics(nil, for: service)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        for characteristic in service.characteristics ?? [] {
            switch characteristic.uuid {
            case Self.audioCodec:
                // Audio is subscribed once the codec is known, so the
                // CONNECTED record always comes before this connection's audio.
                peripheral.readValue(for: characteristic)
            case Self.buttonEvent:
                peripheral.setNotifyValue(true, for: characteristic)
            case Self.timeWrite:
                // The pendant only stores audio while disconnected if its
                // clock has been set; Omi's app sets it on every connect.
                let now = UInt32(Date().timeIntervalSince1970).littleEndian
                peripheral.writeValue(withUnsafeBytes(of: now) { Data($0) }, for: characteristic, type: .withResponse)
            case Self.micGain:
                gain = characteristic
                peripheral.readValue(for: characteristic)
            case Self.storageControl:
                storage = characteristic
                peripheral.setNotifyValue(true, for: characteristic)
            default:
                break
            }
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard let value = characteristic.value else { return }
        switch characteristic.uuid {
        case Self.audioData:
            spool?.append(kind: Spool.audio, data: value)
            count(value)
        case Self.buttonEvent:
            spool?.append(kind: Spool.button, data: value)
            taps += 1
        case Self.audioCodec:
            log.info("codec \(value.first.map { "\($0)" } ?? "?")")
            codec = value
            spool?.append(kind: Spool.connected, data: value)
            let data = characteristic.service?.characteristics?.first { $0.uuid == Self.audioData }
            if let data { peripheral.setNotifyValue(true, for: data) }
            askWhatsStored()
        case Self.micGain:
            if let level = value.first { reconcile(level, peripheral, characteristic) }
        case Self.storageControl:
            storageNotified(value)
        default:
            break
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic,
                    error: Error?) {
        guard characteristic.uuid == Self.storageControl else { return }
        if let error {
            log.error("subscribing to the pendant's storage failed: \(error)")
            return
        }
        storageNotifying = characteristic.isNotifying
        askWhatsStored()
    }

    /// Make the pendant's mic gain match `muted`. A level of 0 while not
    /// muted is a mute left behind (an unmute that never reached the
    /// pendant), so it's undone too.
    private func reconcile(_ level: UInt8, _ peripheral: CBPeripheral, _ characteristic: CBCharacteristic) {
        if muted, level != 0 {
            UserDefaults.standard.set(Int(level), forKey: Self.gainBeforeMuteKey)
            peripheral.writeValue(Data([0]), for: characteristic, type: .withResponse)
        } else if !muted, level == 0 {
            let saved = UserDefaults.standard.object(forKey: Self.gainBeforeMuteKey) as? Int
            let restore = saved.flatMap { UInt8(exactly: $0) } ?? Self.defaultGain
            peripheral.writeValue(Data([restore]), for: characteristic, type: .withResponse)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        switch characteristic.uuid {
        case Self.timeWrite:
            if let error { log.error("setting the pendant's clock failed: \(error)") }
        case Self.micGain:
            if let error {
                log.error("setting the pendant's mic gain failed: \(error)")
                return
            }
            log.info("pendant \(self.muted ? "muted" : "unmuted")")
            // Only once the pendant has the level back, so a failed unmute
            // still restores it on the next connect.
            if !muted { UserDefaults.standard.removeObject(forKey: Self.gainBeforeMuteKey) }
        default:
            break
        }
    }

    // MARK: Audio the pendant stored while no phone was connected

    /// The pendant stores audio only while nothing is connected, so all of
    /// it was recorded while away. Remembers the earliest start of an away
    /// stretch not yet downloaded: Pause needs it, and so does the NAS, to
    /// tell a stored timestamp from a clock gone wrong.
    private func markAway() {
        guard UserDefaults.standard.object(forKey: Self.awaySinceKey) == nil else { return }
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: Self.awaySinceKey)
    }

    private var awaySince: Date? {
        (UserDefaults.standard.object(forKey: Self.awaySinceKey) as? Double).map(Date.init(timeIntervalSince1970:))
    }

    /// Start a download once the codec is known (each stored record carries
    /// it) and the pendant can answer; the answer is an info notification.
    private func askWhatsStored() {
        guard codec != nil, storageNotifying, !downloading else { return }
        command([0x10])
    }

    private func command(_ bytes: [UInt8]) {
        guard let storage, let peripheral, peripheral.state == .connected else { return }
        peripheral.writeValue(Data(bytes), for: storage, type: .withResponse)
    }

    /// Every storage notification starts with its type; integers are big-endian.
    private func storageNotified(_ value: Data) {
        guard let type = value.first else { return }
        switch type {
        case 0x02:  // info: read seq u64, write seq u64, ...
            guard value.count >= 17 else { return }
            let first = bigEndian(value, 1, 8), end = bigEndian(value, 9, 8)
            guard end > first else {
                stored = "nothing stored on the pendant"
                UserDefaults.standard.removeObject(forKey: Self.awaySinceKey)
                return
            }
            log.info("pendant holds \(end - first) stored packets from seq \(first)")
            downloading = true
            // No count: everything from `first`. The pendant frees what it
            // has sent as it goes, so nothing can be left for a second try.
            command([0x11] + (0..<8).map { UInt8(truncatingIfNeeded: first >> (56 - 8 * $0)) })
        case 0x05:  // read begin: start seq u64, packet count u32
            guard value.count >= 13 else { return }
            storedSeq = bigEndian(value, 1, 8)
            storedTotal = UInt32(bigEndian(value, 9, 4))
            storedBuffer = Data()
            storedReceived = 0
            pause?.beginDownload(awaySince: awaySince)
            stored = "downloading \(storedTotal) stored packets"
        case 0x03:  // data: the next bytes of the packet stream, cut anywhere
            storedBuffer.append(value.dropFirst())
            spoolStoredPackets()
        case 0x04:  // done: status u8, next seq u64
            let status = value.count > 1 ? value[value.startIndex + 1] : 0
            log.info("stored audio download done: status \(status), \(self.storedReceived) packets")
            endDownload()
            if status == 0 {
                stored = "downloaded \(storedReceived) stored packets at \(Date().formatted(date: .omitted, time: .shortened))"
                askWhatsStored()  // confirms the pendant is empty
            } else {
                retryLater("download ended with status \(status)")
            }
        case 0x01:  // ack: status u8; only errors come this way
            let status = value.count > 1 ? value[value.startIndex + 1] : 0
            guard status != 0 else { return }
            endDownload()
            // 9: the SD card is still remounting after the connect.
            retryLater("pendant storage answered \(status)")
        default:
            break
        }
    }

    /// Spool each whole packet as it arrives: the pendant frees packets once
    /// they're sent, so this is the only copy. A packet cut short by a
    /// disconnect is sent again on the next download.
    private func spoolStoredPackets() {
        guard let codec = codec?.first else { return }
        let away = awaySince?.timeIntervalSince1970 ?? 0
        while storedBuffer.count >= Self.storedPacketBytes {
            var record = Data([codec])
            withUnsafeBytes(of: storedSeq.littleEndian) { record.append(contentsOf: $0) }
            withUnsafeBytes(of: away.bitPattern.littleEndian) { record.append(contentsOf: $0) }
            record.append(storedBuffer.prefix(Self.storedPacketBytes))
            pause?.storedTarget?.append(kind: Spool.stored, data: record)
            storedBuffer = Data(storedBuffer.dropFirst(Self.storedPacketBytes))
            storedSeq += 1
            storedReceived += 1
            storedThisMinute += 1
        }
        stored = "downloading: \(storedReceived) of \(storedTotal) stored packets"
    }

    private func endDownload() {
        if downloading { pause?.endDownload() }
        downloading = false
        storedBuffer = Data()
    }

    private func retryLater(_ why: String) {
        log.error("\(why); asking again in 30 s")
        stored = "\(why); retrying"
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { [weak self] in self?.askWhatsStored() }
    }

    private func bigEndian(_ data: Data, _ offset: Int, _ count: Int) -> UInt64 {
        data.dropFirst(offset).prefix(count).reduce(0) { $0 << 8 | UInt64($1) }
    }

    private func count(_ value: Data) {
        guard value.count >= 3 else { return }
        let index = UInt16(value[value.startIndex]) | UInt16(value[value.startIndex + 1]) << 8
        if let last = lastIndex, index != last &+ 1 {
            lost += Int(index &- last &- 1)
        }
        lastIndex = index
        packets += 1
    }
}
