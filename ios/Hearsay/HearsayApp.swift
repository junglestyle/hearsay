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
    let buttons: Buttons
    let uploader: Uploader
    let location: Location

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
        buttons = Buttons(pendant: pendant, pause: pause)
        uploader = Uploader(spool: spool)
        location = Location(spool: spool)

        // Uploads cover a minute each; the NAS reprocesses hourly, so there's
        // no point in smaller ones. Timers only fire while the app runs,
        // which in the background is while the pendant is sending.
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [spool, pause, pendant, location] _ in
            spool.sync()
            pause.window?.sync()
            location.tick(recording: !pause.paused && !pendant.muted)
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
            TabView {
                StatusView(pendant: recorder.pendant, pause: recorder.pause, buttons: recorder.buttons,
                           uploader: recorder.uploader)
                    .tabItem { Label("Recorder", systemImage: "waveform") }
                VoicesView()
                    .tabItem { Label("Voices", systemImage: "person.wave.2") }
            }
        }
    }
}

struct StatusView: View {
    @ObservedObject var pendant: Pendant
    @ObservedObject var pause: Pause
    @ObservedObject var buttons: Buttons
    @ObservedObject var uploader: Uploader
    @State private var url = Settings.urlText
    @State private var token = ""
    @State private var portal = Settings.portalText
    @State private var confirmForget = false
    @State private var led = 50.0

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack {
                        Circle().fill(pendant.state == "connected" ? Color.green : Color.secondary)
                            .frame(width: 8, height: 8)
                        Text(pendant.state)
                        Spacer()
                        if let battery = pendant.battery {
                            Label("\(battery)%", systemImage: batterySymbol(battery))
                                .labelStyle(.titleAndIcon)
                                .foregroundStyle(.secondary)
                        }
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        if !pendant.lastMinute.isEmpty { Text("Last minute: \(pendant.lastMinute)") }
                        if !pendant.stored.isEmpty { Text("Stored while away: \(pendant.stored)") }
                        Text("Uploads: \(uploader.pending) waiting, \(uploader.lastResult)")
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }

                Section {
                    HStack(spacing: 12) {
                        control(pause.paused ? "Resume" : "Pause",
                                symbol: pause.paused ? "play.fill" : "pause.fill", on: pause.paused) {
                            if pause.paused { pause.resume() } else { pause.pause() }
                        }
                        control(pendant.muted ? "Unmute" : "Mute",
                                symbol: pendant.muted ? "mic.fill" : "mic.slash.fill", on: pendant.muted) {
                            pendant.setMuted(!pendant.muted)
                        }
                    }
                    .listRowInsets(EdgeInsets(top: 10, leading: 12, bottom: 10, trailing: 12))
                    Toggle("Keep paused audio on this phone", isOn: Binding(get: { pause.keep },
                                                                            set: { pause.setKeep($0) }))
                } footer: {
                    Text(recordingStatus)
                }

                if !pause.windows.isEmpty {
                    Section {
                        ForEach(pause.windows) { window in
                            NavigationLink {
                                WindowView(window: window, pause: pause, uploader: uploader)
                            } label: {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(timeRange(window))
                                    Text(details(window)).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            .swipeActions {
                                Button("Delete", role: .destructive) { pause.delete(window) }
                                Button("Upload") {
                                    pause.upload(window)
                                    uploader.drain()
                                }
                                .tint(.blue)
                            }
                        }
                    } header: {
                        Text("Paused audio to review")
                    } footer: {
                        Text("Tap to listen and keep a stretch. Swipe to upload or delete all of it.")
                    }
                }

                Section("Pendant") {
                    if pendant.led != nil {
                        HStack {
                            Image(systemName: "lightbulb").foregroundStyle(.secondary)
                            Slider(value: $led, in: 0...100, step: 5) { editing in
                                if !editing { pendant.setLED(Int(led)) }
                            }
                        }
                        .onAppear { led = Double(pendant.led ?? 50) }
                        .onChange(of: pendant.led) { _, level in if let level { led = Double(level) } }
                    }
                    Picker("Single tap", selection: $buttons.single) {
                        ForEach(Buttons.Action.allCases) { Text($0.label).tag($0) }
                    }
                    Picker("Double tap", selection: $buttons.double) {
                        ForEach(Buttons.Action.allCases) { Text($0.label).tag($0) }
                    }
                    DisclosureGroup("Buzzes") {
                        Text("Keep: one short. End: one medium. Pause: one long; resume: two short. Mute: two long; unmute: three short. Holding the button 3 s turns the pendant off.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Button("Use a different pendant", role: .destructive) { confirmForget = true }
                        .confirmationDialog("Forget this pendant and connect to the next one found?",
                                            isPresented: $confirmForget, titleVisibility: .visible) {
                            Button("Forget", role: .destructive) { pendant.forget() }
                        }
                }

                Section {
                    Button("Send uploads now") { uploader.drain() }
                    DisclosureGroup("Server") {
                        TextField("http://<nas tailnet ip>:8789/capture", text: $url)
                            .keyboardType(.URL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                        SecureField(Settings.token == nil ? "Token" : "Token (saved; enter to replace)", text: $token)
                        TextField("http://<nas tailnet ip>:8788 (naming portal)", text: $portal)
                            .keyboardType(.URL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                        Button("Save") {
                            Settings.save(url: url, token: token)
                            Settings.save(portal: portal)
                            token = ""
                            uploader.drain()
                        }
                    }
                } footer: {
                    Text("install/ios.sh prints the capture URL, token and portal URL. The phone must be on the tailnet.")
                }
            }
            .navigationTitle("Hearsay")
        }
    }

    /// A large toggle-like button; filled while its state is on.
    private func control(_ title: String, symbol: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: symbol)
                Text(title)
            }
            .frame(maxWidth: .infinity, minHeight: 36)
        }
        .buttonStyle(.borderedProminent)
        .tint(on ? .orange : .accentColor)
    }

    private var recordingStatus: String {
        let mic = pendant.muted ? "Mic muted on the pendant, even out of range." : ""
        guard pause.paused else { return pendant.muted ? mic : "Recording. Paused audio never reaches Hearsay." }
        let paused = pause.keep ? "Paused: kept on this phone for 30 days." : "Paused: dropped as it arrives."
        return [paused, mic].filter { !$0.isEmpty }.joined(separator: " ")
    }

    private func batterySymbol(_ level: Int) -> String {
        if pendant.isCharging == true { return "battery.100.bolt" }
        return "battery.\([0, 25, 50, 75, 100].min { abs($0 - level) < abs($1 - level) }!)"
    }

    private func timeRange(_ window: Pause.Window) -> String {
        let start = window.start.formatted(.dateTime.month(.abbreviated).day().hour().minute())
        return "\(start) – \(window.end.formatted(date: .omitted, time: .shortened))"
    }

    private func details(_ window: Pause.Window) -> String {
        let length = Duration.seconds(window.end.timeIntervalSince(window.start))
            .formatted(.units(allowed: [.hours, .minutes, .seconds], width: .narrow, maximumUnitCount: 2))
        let size = ByteCountFormatter.string(fromByteCount: Int64(window.bytes), countStyle: .file)
        let expires = window.end.addingTimeInterval(Pause.retention).formatted(.dateTime.month(.abbreviated).day())
        return "\(length), \(size), deleted \(expires)"
    }
}
