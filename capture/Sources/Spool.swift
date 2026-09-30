import Foundation

/// Records on local disk until the NAS has them.
///
/// Everything the pendant sends is appended to current.rec as it arrives, in
/// the upload format hearsay/capture.py reads: per record, f64 LE arrival
/// time (unix seconds), u8 kind, u16 LE length, then the bytes. seal() closes
/// the file and renames it to <millis>-<uuid>.upload; the Uploader deletes an
/// upload only once the NAS has answered 200. So a crash or an offline NAS
/// loses at most the last unsynced second.
final class Spool {
    static let connected: UInt8 = 1
    static let audio: UInt8 = 2
    static let button: UInt8 = 3

    let dir: URL
    private let current: URL
    private var handle: FileHandle?
    private var dirty = false

    init(dir: URL) throws {
        self.dir = dir
        current = dir.appendingPathComponent("current.rec")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        // Left over from a previous run: send what it has.
        seal()
    }

    func append(kind: UInt8, data: Data) {
        var record = Data()
        withUnsafeBytes(of: Date().timeIntervalSince1970.bitPattern.littleEndian) { record.append(contentsOf: $0) }
        record.append(kind)
        withUnsafeBytes(of: UInt16(data.count).littleEndian) { record.append(contentsOf: $0) }
        record.append(data)
        do {
            if handle == nil {
                FileManager.default.createFile(atPath: current.path, contents: nil)
                handle = try FileHandle(forWritingTo: current)
                try handle?.seekToEnd()
            }
            try handle?.write(contentsOf: record)
            dirty = true
        } catch {
            log("spool write failed: \(error)")
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
        let millis = Int64(Date().timeIntervalSince1970 * 1000)
        let upload = dir.appendingPathComponent("\(millis)-\(UUID().uuidString.lowercased()).upload")
        do {
            try whole.write(to: upload, options: .atomic)
            try FileManager.default.removeItem(at: current)
        } catch {
            log("spool seal failed: \(error)")
        }
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
