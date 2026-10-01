import AVFoundation
import Combine
import SwiftUI

/// Listen to a paused window, then upload one stretch of it (deleting the
/// rest) or delete it all. Positions are seconds of audio, skipping silence;
/// labels show the time of day.
struct WindowView: View {
    let window: Pause.Window
    @ObservedObject var pause: Pause
    let uploader: Uploader

    @Environment(\.dismiss) private var dismiss
    @State private var audio: WindowAudio?
    @State private var failed: String?
    @State private var player: AVAudioPlayer?
    @State private var position = 0.0
    @State private var playing = false
    @State private var from = 0.0
    @State private var to = 0.0
    @State private var confirmUpload = false
    @State private var confirmDelete = false
    private let tick = Timer.publish(every: 0.2, on: .main, in: .common).autoconnect()

    var body: some View {
        Form {
            if let audio, audio.duration > 0 {
                Section("Listen") {
                    Slider(value: $position, in: 0...audio.duration) { editing in
                        if !editing { player?.currentTime = position }
                    }
                    HStack {
                        Button(playing ? "Pause" : "Play") { playing ? stop() : play() }
                        Spacer()
                        Text(clock(audio, position)).monospacedDigit()
                    }
                }
                Section {
                    Slider(value: $from, in: 0...audio.duration)
                    HStack {
                        Text("From \(clock(audio, from))").monospacedDigit()
                        Spacer()
                        Button("Here") { from = min(position, to) }.buttonStyle(.borderless)
                        Button("Play") { position = from; player?.currentTime = from; play() }.buttonStyle(.borderless)
                    }
                    Slider(value: $to, in: 0...audio.duration)
                    HStack {
                        Text("To \(clock(audio, to))").monospacedDigit()
                        Spacer()
                        Button("Here") { to = max(position, from) }.buttonStyle(.borderless)
                    }
                    Button("Upload this stretch") { confirmUpload = true }
                        .disabled(to <= from)
                        .confirmationDialog("Upload \(clock(audio, from))–\(clock(audio, to)) and delete the rest of this window?",
                                            isPresented: $confirmUpload, titleVisibility: .visible) {
                            Button("Upload") { uploadSelection(audio) }
                        }
                } header: {
                    Text("Keep")
                } footer: {
                    Text("\(minutes(to - from)) of \(minutes(audio.duration)) of audio. The rest is deleted from this phone.")
                }
            } else if let failed {
                Text("Can't play this window: \(failed)")
            } else if audio != nil {
                Text("No audio in this window")
            } else {
                ProgressView("Decoding")
            }
            Section {
                Button("Delete this window", role: .destructive) { confirmDelete = true }
                    .confirmationDialog("Delete all of this window's audio?", isPresented: $confirmDelete,
                                        titleVisibility: .visible) {
                        Button("Delete", role: .destructive) {
                            pause.delete(window)
                            dismiss()
                        }
                    }
            }
        }
        .navigationTitle(window.start.formatted(date: .abbreviated, time: .shortened))
        .onAppear(perform: load)
        .onDisappear {
            player?.stop()
            audio?.close()
        }
        .onReceive(tick) { _ in
            guard let player, playing else { return }
            position = player.currentTime
            if !player.isPlaying { playing = false }
        }
    }

    private func load() {
        guard audio == nil else { return }
        let dir = window.dir
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Result { try WindowAudio(dir: dir) }
            DispatchQueue.main.async {
                switch result {
                case .success(let decoded):
                    audio = decoded
                    to = decoded.duration
                    player = try? AVAudioPlayer(contentsOf: decoded.wav)
                case .failure(let error):
                    failed = error.localizedDescription
                }
            }
        }
    }

    private func play() {
        try? AVAudioSession.sharedInstance().setCategory(.playback)
        try? AVAudioSession.sharedInstance().setActive(true)
        player?.play()
        playing = true
    }

    private func stop() {
        player?.pause()
        playing = false
    }

    private func uploadSelection(_ audio: WindowAudio) {
        player?.stop()
        pause.upload(window, cropped: audio.cropped(from: from, to: to))
        uploader.drain()
        dismiss()
    }

    private func clock(_ audio: WindowAudio, _ position: Double) -> String {
        audio.wallTime(position).formatted(date: .omitted, time: .standard)
    }

    private func minutes(_ seconds: Double) -> String {
        Duration.seconds(max(seconds, 0)).formatted(.time(pattern: .minuteSecond))
    }
}
