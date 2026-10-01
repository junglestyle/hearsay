import Foundation

/// Pausing: while paused, nothing the pendant sends reaches the NAS. The
/// pendant stays connected and subscribed (its firmware throws audio away
/// while connected but unsubscribed, rather than storing it), and the app
/// routes what arrives elsewhere: nowhere, or, with keep on, into a paused
/// window on the phone.
///
/// A window is its own Spool under paused/<start millis>/, so its files are
/// already uploads: keeping one moves them into the upload spool, and the NAS
/// places them by their own arrival times. Windows not kept are deleted
/// `retention` after they end.
final class Pause: ObservableObject {
    static let retention: TimeInterval = 30 * 86400
    private static let pausedKey = "paused"
    private static let keepKey = "keepPaused"
    private static let windowKey = "pausedWindow"

    struct Window: Identifiable {
        let dir: URL
        let start: Date
        let end: Date
        let bytes: Int
        var id: URL { dir }
    }

    @Published private(set) var paused: Bool
    @Published private(set) var keep: Bool
    /// Finished windows, oldest first; the one being recorded isn't listed.
    @Published private(set) var windows: [Window] = []

    private(set) var window: Spool?
    private let root: URL
    private let spool: Spool
    private let pendant: Pendant

    init(root: URL, spool: Spool, pendant: Pendant) {
        self.root = root
        self.spool = spool
        self.pendant = pendant
        paused = UserDefaults.standard.bool(forKey: Self.pausedKey)
        keep = UserDefaults.standard.bool(forKey: Self.keepKey)
        if paused && keep {
            // Relaunched mid-pause: carry on in the same window.
            let name = UserDefaults.standard.string(forKey: Self.windowKey) ?? Self.newWindowName()
            window = openWindow(name)
        }
        pendant.route(to: paused ? window : spool)
        expire()
    }

    func pause() {
        guard !paused else { return }
        paused = true
        UserDefaults.standard.set(true, forKey: Self.pausedKey)
        spool.seal()
        if keep { window = openWindow(Self.newWindowName()) }
        pendant.route(to: window)
        log.info("paused, \(self.keep ? "keeping on the phone" : "dropping")")
    }

    func resume() {
        guard paused else { return }
        paused = false
        UserDefaults.standard.set(false, forKey: Self.pausedKey)
        endWindow()
        pendant.route(to: spool)
        log.info("resumed")
    }

    func setKeep(_ on: Bool) {
        guard on != keep else { return }
        keep = on
        UserDefaults.standard.set(on, forKey: Self.keepKey)
        guard paused else { return }
        if on {
            window = openWindow(Self.newWindowName())
        } else {
            endWindow()
        }
        pendant.route(to: window)
    }

    /// Upload a window like normal audio.
    func upload(_ window: Window) {
        do {
            for file in try Spool(dir: window.dir).pending() {
                try FileManager.default.moveItem(at: file, to: spool.dir.appendingPathComponent(file.lastPathComponent))
            }
            try FileManager.default.removeItem(at: window.dir)
        } catch {
            log.error("keeping paused window \(window.dir.lastPathComponent) failed: \(error)")
        }
        refresh()
    }

    func delete(_ window: Window) {
        try? FileManager.default.removeItem(at: window.dir)
        refresh()
    }

    /// Delete windows past retention; called every minute.
    func expire() {
        refresh()
        let cutoff = Date().addingTimeInterval(-Self.retention)
        for window in windows where window.end < cutoff {
            log.info("deleting paused window \(window.dir.lastPathComponent), past retention")
            try? FileManager.default.removeItem(at: window.dir)
        }
        refresh()
    }

    private func refresh() {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: root.path)) ?? []
        windows = names.sorted().compactMap { name in
            guard name != window?.dir.lastPathComponent, let millis = Int64(name) else { return nil }
            let dir = root.appendingPathComponent(name)
            // Opening it seals whatever a crash left in current.rec.
            guard let files = try? Spool(dir: dir).pending() else { return nil }
            var end = Date(timeIntervalSince1970: Double(millis) / 1000)
            var bytes = 0
            for file in files {
                let values = try? file.resourceValues(forKeys: [.contentModificationDateKey, .fileSizeKey])
                end = max(end, values?.contentModificationDate ?? end)
                bytes += values?.fileSize ?? 0
            }
            return Window(dir: dir, start: Date(timeIntervalSince1970: Double(millis) / 1000), end: end, bytes: bytes)
        }
    }

    private func endWindow() {
        window?.seal()
        window = nil
        UserDefaults.standard.removeObject(forKey: Self.windowKey)
        refresh()
    }

    /// nil (so paused audio is dropped) if the window can't be made.
    private func openWindow(_ name: String) -> Spool? {
        do {
            let opened = try Spool(dir: root.appendingPathComponent(name))
            UserDefaults.standard.set(name, forKey: Self.windowKey)
            return opened
        } catch {
            log.error("can't keep paused audio in \(name): \(error); dropping it")
            return nil
        }
    }

    private static func newWindowName() -> String {
        "\(Int64(Date().timeIntervalSince1970 * 1000))"
    }
}
