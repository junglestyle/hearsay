// hearsay-mac: a menu-bar app that records the Mac's mic and system audio as
// two channels and uploads them to Hearsay's capture receiver on the NAS,
// over the tailnet. See Spool for the upload format and hearsay/capture.py
// for how the NAS reads it.
//
// Recording starts from the menu, or, when a Zoom call starts, after the app
// asks: recording a call can need everyone's consent, so it never starts on
// its own. A call recording taps Zoom's audio only and stops with the call.
//
// Config comes from an env file outside the repo (install/mac.sh writes it):
//   HEARSAY_CAPTURE_URL    http://<nas tailnet ip>:8789/capture
//   HEARSAY_CAPTURE_TOKEN  bearer token from the NAS's capture.env
//   HEARSAY_CAPTURE_SPOOL  optional; default ~/.local/share/hearsay/mac-spool

import AppKit
import CoreAudio

func log(_ message: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    print("\(stamp) \(message)")
    fflush(stdout)
}

func readEnv(_ path: String) -> [String: String] {
    guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return [:] }
    var env: [String: String] = [:]
    for line in text.split(separator: "\n") {
        guard !line.hasPrefix("#"), let eq = line.firstIndex(of: "=") else { continue }
        env[String(line[..<eq])] = String(line[line.index(after: eq)...])
    }
    return env
}

final class Recorder: NSObject, NSApplicationDelegate {
    private let spool: Spool
    private let uploader: Uploader
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let mic = Mic()
    private let system = SystemAudio()
    private var channels: [Channel] = []
    private var recording: String?  // "call" or "manual" while recording
    private var inCall = false

    init(spool: Spool, uploader: Uploader) {
        self.spool = spool
        self.uploader = uploader
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        refresh()
        // Uploads cover a minute each; the NAS reprocesses hourly, so there's
        // no point in smaller ones.
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in self.spool.queue.async { self.spool.sync() } }
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in self.spool.queue.async { self.spool.seal() } }
        Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { _ in
            self.uploader.drain()
            self.refresh()
        }
        Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in self.watchCalls() }
        uploader.drain()
    }

    private func watchCalls() {
        let active = zoomCallActive()
        defer { inCall = active }
        if active && !inCall && recording == nil {
            ask()
        } else if !active && inCall && recording == "call" {
            stop()
        }
    }

    private func ask() {
        let alert = NSAlert()
        alert.messageText = "Record this Zoom call?"
        alert.informativeText = "Everyone on the call may need to agree to being recorded."
        alert.addButton(withTitle: "Record")
        alert.addButton(withTitle: "Don't Record")
        NSApp.activate(ignoringOtherApps: true)
        let answer = alert.runModal()
        // The call may have ended while the question was up.
        guard answer == .alertFirstButtonReturn, zoomCallActive(), recording == nil else {
            log("call not recorded")
            return
        }
        start(kind: "call", processes: zoomProcesses())
    }

    private func start(kind: String, processes: [AudioObjectID]) {
        do {
            let micChannel = try Channel(kind: Spool.mic, spool: spool)
            let systemChannel = try Channel(kind: Spool.system, spool: spool)
            try system.start(processes: processes, into: systemChannel)
            do {
                try mic.start(into: micChannel)
            } catch {
                system.stop()
                throw error
            }
            channels = [micChannel, systemChannel]
            recording = kind
            log("recording (\(kind))")
        } catch {
            log("can't record: \(error)")
            let alert = NSAlert()
            alert.messageText = "Hearsay can't record"
            alert.informativeText = "\(error)"
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
        }
        refresh()
    }

    @objc private func stop() {
        guard recording != nil else { return }
        mic.stop()
        system.stop()
        channels.forEach { $0.finish() }
        let finished = channels
        channels = []
        recording = nil
        spool.queue.async {
            self.spool.seal()
            let written = finished.map { String(format: "%.1f", $0.seconds) }
            log("stopped: mic \(written[0])s, system \(written[1])s")
            DispatchQueue.main.async { self.uploader.drain() }
        }
        refresh()
    }

    @objc private func recordManually() {
        start(kind: "manual", processes: [])
    }

    private func refresh() {
        let symbol = recording == nil ? "mic" : "record.circle.fill"
        item.button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Hearsay")
        let menu = NSMenu()
        switch recording {
        case "call": menu.addItem(withTitle: "Recording this call", action: nil, keyEquivalent: "")
        case "manual": menu.addItem(withTitle: "Recording", action: nil, keyEquivalent: "")
        default: menu.addItem(withTitle: "Not recording", action: nil, keyEquivalent: "")
        }
        menu.addItem(.separator())
        if recording == nil {
            menu.addItem(withTitle: "Record", action: #selector(recordManually), keyEquivalent: "").target = self
        } else {
            menu.addItem(withTitle: "Stop", action: #selector(stop), keyEquivalent: "").target = self
        }
        let waiting = spool.pending().count
        if waiting > 0 {
            menu.addItem(withTitle: "\(waiting) upload(s) waiting for the NAS", action: nil, keyEquivalent: "")
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = menu
    }

    func applicationWillTerminate(_ notification: Notification) {
        stop()
        spool.queue.sync { spool.seal() }
    }
}

let home = FileManager.default.homeDirectoryForCurrentUser.path
let envPath = CommandLine.arguments.dropFirst().first ?? "\(home)/.config/hearsay/mac.env"
let env = readEnv(envPath)
guard let urlText = env["HEARSAY_CAPTURE_URL"], let url = URL(string: urlText),
      let token = env["HEARSAY_CAPTURE_TOKEN"], !token.isEmpty else {
    log("missing HEARSAY_CAPTURE_URL or HEARSAY_CAPTURE_TOKEN in \(envPath); run install/mac.sh")
    exit(1)
}
let spoolDir = URL(fileURLWithPath: env["HEARSAY_CAPTURE_SPOOL"] ?? "\(home)/.local/share/hearsay/mac-spool")
let spool: Spool
do {
    spool = try Spool(dir: spoolDir)
} catch {
    log("can't use the spool at \(spoolDir.path): \(error)")
    exit(1)
}
log("spooling to \(spoolDir.path), uploading to \(url)")

let app = NSApplication.shared
let recorder = Recorder(spool: spool, uploader: Uploader(spool: spool, url: url, token: token))
app.delegate = recorder
app.setActivationPolicy(.accessory)
app.run()
