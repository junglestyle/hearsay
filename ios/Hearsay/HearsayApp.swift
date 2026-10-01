// Hearsay's iPhone app: records the Omi pendant over BLE and uploads what it
// sends to Hearsay's capture receiver on the NAS, over the tailnet. See Spool
// for the upload format and hearsay/capture.py for how the NAS reads it.

import os
import SwiftUI

let log = Logger(subsystem: "hearsay", category: "capture")

/// The recording runs from launch, whether or not a screen is shown: iOS
/// relaunches the app in the background for the pendant (state restoration).
final class Recorder {
    static let shared = Recorder()

    let spool: Spool
    let pendant: Pendant
    let pause: Pause
    let uploader: Uploader

    private init() {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = support.appendingPathComponent("spool")
        do {
            spool = try Spool(dir: dir)
        } catch {
            fatalError("can't use the spool at \(dir.path): \(error)")
        }
        pendant = Pendant()
        pause = Pause(root: support.appendingPathComponent("paused"), spool: spool, pendant: pendant)
        uploader = Uploader(spool: spool)

        // Uploads cover a minute each; the NAS reprocesses hourly, so there's
        // no point in smaller ones. Timers only fire while the app runs,
        // which in the background is while the pendant is sending.
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [spool, pause] _ in
            spool.sync()
            pause.window?.sync()
        }
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [spool, pendant, pause] _ in
            spool.seal()
            pause.window?.seal()
            pause.expire()
            pendant.report()
        }
        Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [uploader] _ in uploader.drain() }
        uploader.drain()
    }
}

@main
struct HearsayApp: App {
    private let recorder = Recorder.shared

    var body: some Scene {
        WindowGroup {
            StatusView(pendant: recorder.pendant, pause: recorder.pause, uploader: recorder.uploader)
        }
    }
}

struct StatusView: View {
    @ObservedObject var pendant: Pendant
    @ObservedObject var pause: Pause
    @ObservedObject var uploader: Uploader
    @State private var url = Settings.urlText
    @State private var token = ""
    @State private var confirmForget = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Pendant") {
                    Text(pendant.state)
                    if !pendant.lastMinute.isEmpty {
                        Text("Last minute: \(pendant.lastMinute)")
                    }
                    Button("Use a different pendant", role: .destructive) { confirmForget = true }
                        .confirmationDialog("Forget this pendant and connect to the next one found?",
                                            isPresented: $confirmForget, titleVisibility: .visible) {
                            Button("Forget", role: .destructive) { pendant.forget() }
                        }
                }
                Section {
                    Toggle("Mute the microphone", isOn: Binding(get: { pendant.muted },
                                                                set: { pendant.setMuted($0) }))
                } footer: {
                    Text("Turns the pendant's mic off in hardware. It stays off, even out of range, until unmuted here.")
                }
                Section {
                    Text(pauseStatus)
                    Button(pause.paused ? "Resume" : "Pause") {
                        if pause.paused { pause.resume() } else { pause.pause() }
                    }
                    Toggle("Keep paused audio on this phone", isOn: Binding(get: { pause.keep },
                                                                            set: { pause.setKeep($0) }))
                } header: {
                    Text("Pause")
                } footer: {
                    Text("Paused audio never reaches Hearsay. Kept, it stays on this phone for 30 days, and you can upload a paused stretch until then.")
                }
                if !pause.windows.isEmpty {
                    Section {
                        ForEach(pause.windows) { window in
                            Text(describe(window))
                                .swipeActions {
                                    Button("Delete", role: .destructive) { pause.delete(window) }
                                    Button("Upload") {
                                        pause.upload(window)
                                        uploader.drain()
                                    }
                                }
                        }
                    } header: {
                        Text("Paused audio on this phone")
                    } footer: {
                        Text("Swipe to upload or delete.")
                    }
                }
                Section("Uploads") {
                    Text("\(uploader.pending) waiting on the phone")
                    Text(uploader.lastResult)
                    Button("Send now") { uploader.drain() }
                }
                Section {
                    TextField("http://<nas tailnet ip>:8789/capture", text: $url)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField(Settings.token == nil ? "Token" : "Token (saved; enter to replace)", text: $token)
                    Button("Save") {
                        Settings.save(url: url, token: token)
                        token = ""
                        uploader.drain()
                    }
                } header: {
                    Text("Capture receiver")
                } footer: {
                    Text("install/ios.sh prints both. The phone must be on the tailnet.")
                }
            }
            .navigationTitle("Hearsay")
        }
    }

    private var pauseStatus: String {
        guard pause.paused else { return "Recording" }
        return pause.keep ? "Paused: keeping it on this phone only" : "Paused: dropping it as it arrives"
    }

    private func describe(_ window: Pause.Window) -> String {
        let start = window.start.formatted(date: .abbreviated, time: .shortened)
        let end = window.end.formatted(date: .omitted, time: .shortened)
        let size = ByteCountFormatter.string(fromByteCount: Int64(window.bytes), countStyle: .file)
        return "\(start) – \(end), \(size)"
    }
}
