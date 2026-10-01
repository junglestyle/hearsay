import Foundation

/// Records on the phone's disk until the NAS has them.
///
/// Everything the pendant sends is appended to current.rec as it arrives, in
/// the upload format hearsay/capture.py reads: per record, f64 LE arrival
/// time (unix seconds), u8 kind, u16 LE length, then the bytes. seal() closes
/// the file and renames it to <millis>-<uuid>.upload; the Uploader deletes an
/// upload only once the NAS has answered 200. Writes reach the kernel at
/// once, so even iOS killing the app loses nothing; only losing power can
/// cost the last unsynced second.
final class Spool {
    static let connected: UInt8 = 1
    static let audio: UInt8 = 2
    static let button: UInt8 = 3
    /// Audio the pendant stored while away: codec u8, the pendant's
    /// sequence number u64 LE, start of the away stretch f64 LE (0 unknown),
    /// then the stored packet as the pendant keeps it.
    static let stored: UInt8 = 4
    /// What the app did about a button event (Buttons): event u8, outcome u8.
    static let action: UInt8 = 5

    let dir: URL
    private let current: URL
    private var handle: FileHandle?
    private var dirty = false

    init(dir: URL) throws {
        self.dir = dir
        current = dir.appendingPathComponent("current.rec")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        // Audio stays inside Hearsay's boundary, which iCloud backups are not.
        var noBackup = URLResourceValues()
        noBackup.isExcludedFromBackup = true
        var excluded = dir
        try excluded.setResourceValues(noBackup)
        // Left over from a previous run: send what it has, and drop a cropped
        // upload that was never finished (Pause.upload(_:cropped:)).
        seal()
        for name in (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? [] where name.hasSuffix(".part") {
            try? FileManager.default.removeItem(at: dir.appendingPathComponent(name))
        }
    }

    struct Record {
        let at: Double
        let kind: UInt8
        let data: Data

        var encoded: Data {
            var out = Data()
            withUnsafeBytes(of: at.bitPattern.littleEndian) { out.append(contentsOf: $0) }
            out.append(kind)
            withUnsafeBytes(of: UInt16(data.count).littleEndian) { out.append(contentsOf: $0) }
            out.append(data)
            return out
        }
    }

    /// The whole records in a sealed file, in the order they arrived.
    static func records(in file: URL) throws -> [Record] {
        let data = try Data(contentsOf: file)
        var out: [Record] = []
        var offset = data.startIndex
        while offset + 11 <= data.endIndex {
            let at = Double(bitPattern: data[offset..<offset + 8].reversed().reduce(0) { $0 << 8 | UInt64($1) })
            let length = Int(data[offset + 9]) | Int(data[offset + 10]) << 8
            guard offset + 11 + length <= data.endIndex else { break }
            out.append(Record(at: at, kind: data[offset + 8], data: Data(data[offset + 11..<offset + 11 + length])))
            offset += 11 + length
        }
        return out
    }

    func append(kind: UInt8, data: Data) {
        let record = Record(at: Date().timeIntervalSince1970, kind: kind, data: data).encoded
        do {
            if handle == nil {
                FileManager.default.createFile(atPath: current.path, contents: nil)
                handle = try FileHandle(forWritingTo: current)
                try handle?.seekToEnd()
            }
            try handle?.write(contentsOf: record)
            dirty = true
        } catch {
            log.error("spool write failed: \(error)")
        }
    }

    /// Flush to disk; called every second.
    func sync() {
        guard dirty else { return }
        try? handle?.synchronize()
        dirty = false
    }

    /// Close current.rec and queue it for upload.
    func seal() {
        sync()
        try? handle?.close()
        handle = nil
        guard let data = FileManager.default.contents(atPath: current.path) else { return }
        // A crash mid-write leaves a partial record, which the NAS would
        // reject along with every other upload: keep whole records only.
        let whole = data.prefix(wholeRecords(data))
        if whole.isEmpty {
            try? FileManager.default.removeItem(at: current)
            return
        }
        let upload = dir.appendingPathComponent(Self.uploadName())
        do {
            try whole.write(to: upload, options: .atomic)
            try FileManager.default.removeItem(at: current)
        } catch {
            log.error("spool seal failed: \(error)")
        }
    }

    /// <millis>-<uuid>.upload: oldest first when sorted, and the name is the
    /// upload's idempotency key.
    static func uploadName() -> String {
        "\(Int64(Date().timeIntervalSince1970 * 1000))-\(UUID().uuidString.lowercased()).upload"
    }

    /// Sealed uploads, oldest first.
    func pending() -> [URL] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
        return names.filter { $0.hasSuffix(".upload") }.sorted().map { dir.appendingPathComponent($0) }
    }

    private func wholeRecords(_ data: Data) -> Int {
        var offset = 0
        while offset + 11 <= data.count {
            let lo = Int(data[data.startIndex + offset + 9]), hi = Int(data[data.startIndex + offset + 10])
            let end = offset + 11 + (lo | hi << 8)
            if end > data.count { break }
            offset = end
        }
        return offset
    }
}
