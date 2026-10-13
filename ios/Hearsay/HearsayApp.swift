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
        location = Location { [spool, pause, pendant] in
            pendant.muted ? nil : pause.paused ? pause.window : spool
        }

        // Uploads cover a minute each; the NAS reprocesses hourly, so there's
        // no point in smaller ones. Timers only fire while the app runs,
        // which in the background is while the pendant is sending.
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [spool, pause, location] _ in
            spool.sync()
            pause.window?.sync()
            location.tick()
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
                           uploader: recorder.uploader, location: recorder.location)
                    .tabItem { Label("Recorder", image: "Voiceprint") }
                VoicesView()
                    .tabItem { Label("Voices", systemImage: "person.wave.2") }
                PlacesView()
                    .tabItem { Label("Places", systemImage: "map") }
            }
            .tint(.ink)
        }
    }
}

/// The orange at the core of the app icon. It means one thing: the pendant's
/// audio is reaching Hearsay right now.
extension Color {
    static let live = Color(red: 1, green: 0x5A / 255, blue: 0x1F / 255)

    /// The icon's rings: the tint for everything interactive, so the orange
    /// stays the only color on screen.
    static let ink = Color(UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 0xE8 / 255, green: 0xE4 / 255, blue: 0xDA / 255, alpha: 1)
            : UIColor(red: 0x1A / 255, green: 0x19 / 255, blue: 0x18 / 255, alpha: 1)
    })
}

struct StatusView: View {
    @ObservedObject var pendant: Pendant
    @ObservedObject var pause: Pause
    @ObservedObject var buttons: Buttons
    @ObservedObject var uploader: Uploader
    @ObservedObject var location: Location

    var body: some View {
        NavigationStack {
            List {
                Section {
                    header
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
                    .listRowSeparator(.hidden)
                } footer: {
                    Text(recordingStatus)
                }

                Section("Activity") {
                    if !pendant.lastMinute.isEmpty { detail("Last minute", pendant.lastMinute) }
                    if !pendant.stored.isEmpty { detail("Stored while away", pendant.stored) }
                    HStack {
                        detail("Uploads", "\(uploader.pending) waiting · \(uploader.lastResult)")
                        Spacer()
                        Button("Send Now") { uploader.drain() }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                    }
                }

                if !pause.windows.isEmpty {
                    Section {
                        ForEach(pause.windows) { window in
                            NavigationLink {
                                WindowView(window: window, pause: pause, uploader: uploader)
                            } label: {
                                detail(timeRange(window), details(window))
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
            }
            .navigationTitle("Hearsay")
            .toolbar {
                NavigationLink {
                    SettingsView(pendant: pendant, pause: pause, buttons: buttons,
                                 uploader: uploader, location: location)
                } label: {
                    Label("Settings", systemImage: "gearshape")
                }
            }
        }
    }

    private var connected: Bool { pendant.state == "connected" }
    private var live: Bool { connected && !pause.paused && !pendant.muted }

    /// What's happening, in one line, then the pendant's own state.
    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: "circle.fill")
                .font(.caption)
                .foregroundStyle(live ? Color.live : Color.secondary.opacity(0.5))
                .symbolEffect(.pulse, isActive: live)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(headline)
                    .font(.title2.weight(.semibold))
                    .contentTransition(.opacity)
                Text(connected ? "Pendant connected" : pendant.state.prefix(1).uppercased() + pendant.state.dropFirst())
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let battery = pendant.battery {
                Label("\(battery)%", systemImage: batterySymbol(battery))
                    .font(.subheadline)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
        .animation(.snappy, value: headline)
        .accessibilityElement(children: .combine)
    }

    private var headline: String {
        if pause.paused { return "Paused" }
        if pendant.muted { return "Muted" }
        return connected ? "Recording" : "Not connected"
    }

    /// Filled while its state is on, so the button shows the state too.
    @ViewBuilder
    private func control(_ title: String, symbol: String, on: Bool, action: @escaping () -> Void) -> some View {
        let button = Button(action: action) {
            Label(title, systemImage: symbol)
                .fontWeight(.medium)
                .frame(maxWidth: .infinity)
        }
        .controlSize(.large)
        .tint(.ink)
        if on {
            button.buttonStyle(.borderedProminent).foregroundStyle(Color(.systemBackground))
        } else {
            button.buttonStyle(.bordered).foregroundStyle(.primary)
        }
    }

    private func detail(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
            Text(value)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .monospacedDigit()
        }
    }

    private var recordingStatus: String {
        let mic = pendant.muted ? "Mic muted on the pendant, even out of range." : ""
        guard pause.paused else { return pendant.muted ? mic : "Paused audio never reaches Hearsay." }
        let paused = pause.keep ? "Paused audio is kept on this phone for 30 days." : "Paused audio is dropped as it arrives."
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

extension Movement.State {
    var label: String {
        switch self {
        case .off: "Not recording"
        case .uncertain: "Unsure"
        case .stationary: "Staying put"
        case .walking: "Walking"
        case .cycling: "Cycling"
        case .driving: "Driving"
        }
    }
}
