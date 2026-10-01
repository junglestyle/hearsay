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
    let uploader: Uploader

    private init() {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("spool")
        do {
            spool = try Spool(dir: dir)
        } catch {
            fatalError("can't use the spool at \(dir.path): \(error)")
        }
        pendant = Pendant(spool: spool)
        uploader = Uploader(spool: spool)

        // Uploads cover a minute each; the NAS reprocesses hourly, so there's
        // no point in smaller ones. Timers only fire while the app runs,
        // which in the background is while the pendant is sending.
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [spool] _ in spool.sync() }
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [spool, pendant] _ in
            spool.seal()
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
            StatusView(pendant: recorder.pendant, uploader: recorder.uploader)
        }
    }
}

struct StatusView: View {
    @ObservedObject var pendant: Pendant
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
}
