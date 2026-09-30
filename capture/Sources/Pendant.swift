import CoreBluetooth
import Foundation

/// The Omi pendant over BLE: find it, stay connected, and spool everything it
/// sends. Nothing is decoded here; the NAS does that from the spooled bytes.
///
/// UUIDs and formats are from Omi's firmware (BasedHardware/omi, MIT),
/// omi/firmware/omi/src/lib/core/transport.c and button.c.
final class Pendant: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    static let audioService = CBUUID(string: "19B10000-E8F2-537E-4F6C-D104768A1214")
    static let audioData = CBUUID(string: "19B10001-E8F2-537E-4F6C-D104768A1214")
    static let audioCodec = CBUUID(string: "19B10002-E8F2-537E-4F6C-D104768A1214")
    static let buttonService = CBUUID(string: "23BA7924-0000-1000-7450-346EAC492E92")
    static let buttonEvent = CBUUID(string: "23BA7925-0000-1000-7450-346EAC492E92")
    static let timeService = CBUUID(string: "19B10030-E8F2-537E-4F6C-D104768A1214")
    static let timeWrite = CBUUID(string: "19B10031-E8F2-537E-4F6C-D104768A1214")

    private let spool: Spool
    private let rememberedID: URL
    private var central: CBCentralManager!
    private var peripheral: CBPeripheral?

    // For the once-a-minute log line.
    private var packets = 0
    private var lost = 0
    private var taps = 0
    private var lastIndex: UInt16?

    init(spool: Spool) {
        self.spool = spool
        rememberedID = spool.dir.appendingPathComponent("pendant-id")
        super.init()
        central = CBCentralManager(delegate: self, queue: .main)
    }

    func report() {
        let state = peripheral?.state == .connected ? "connected" : "not connected"
        log("\(state): \(packets) audio packets, \(lost) lost, \(taps) button events in the last minute")
        packets = 0
        lost = 0
        taps = 0
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        switch central.state {
        case .poweredOn:
            // One pendant: the first one found is remembered, and only it is used.
            if let text = try? String(contentsOf: rememberedID, encoding: .utf8),
               let id = UUID(uuidString: text.trimmingCharacters(in: .whitespacesAndNewlines)),
               let known = central.retrievePeripherals(withIdentifiers: [id]).first {
                connect(known)
            } else {
                log("scanning for the pendant")
                central.scanForPeripherals(withServices: [Self.audioService])
            }
        case .unauthorized:
            log("Bluetooth access denied: allow hearsay-capture in System Settings > Privacy & Security > Bluetooth")
        default:
            log("Bluetooth is \(central.state.rawValue) (not powered on)")
        }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi: NSNumber) {
        central.stopScan()
        try? peripheral.identifier.uuidString.write(to: rememberedID, atomically: true, encoding: .utf8)
        log("found pendant \(peripheral.name ?? "?") \(peripheral.identifier)")
        connect(peripheral)
    }

    private func connect(_ found: CBPeripheral) {
        peripheral = found
        found.delegate = self
        // Doesn't time out: it completes whenever the pendant comes in range.
        central.connect(found)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        log("connected")
        lastIndex = nil
        peripheral.discoverServices([Self.audioService, Self.buttonService, Self.timeService])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        log("connect failed: \(error.map { "\($0)" } ?? "unknown"); retrying")
        central.connect(peripheral)
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral,
                        error: Error?) {
        log("disconnected: \(error.map { "\($0)" } ?? "no error"); waiting for the pendant")
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
            default:
                break
            }
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard let value = characteristic.value else { return }
        switch characteristic.uuid {
        case Self.audioData:
            spool.append(kind: Spool.audio, data: value)
            count(value)
        case Self.buttonEvent:
            spool.append(kind: Spool.button, data: value)
            taps += 1
        case Self.audioCodec:
            log("codec \(value.first.map { "\($0)" } ?? "?")")
            spool.append(kind: Spool.connected, data: value)
            let data = characteristic.service?.characteristics?.first { $0.uuid == Self.audioData }
            if let data { peripheral.setNotifyValue(true, for: data) }
        default:
            break
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        if characteristic.uuid == Self.timeWrite {
            log(error.map { "setting the pendant's clock failed: \($0)" } ?? "set the pendant's clock")
        }
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
