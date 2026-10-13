import SwiftUI

/// What's set once and rarely touched, kept off the Recorder screen so it
/// shows only what's happening now.
struct SettingsView: View {
    @ObservedObject var pendant: Pendant
    @ObservedObject var pause: Pause
    @ObservedObject var buttons: Buttons
    let uploader: Uploader
    @ObservedObject var location: Location
    @State private var url = Settings.urlText
    @State private var token = ""
    @State private var portal = Settings.portalText
    @State private var confirmForget = false
    @State private var led = 50.0

    var body: some View {
        Form {
            Section {
                Picker("Single tap", selection: $buttons.single) {
                    ForEach(Buttons.Action.allCases) { Text($0.label).tag($0) }
                }
                Picker("Double tap", selection: $buttons.double) {
                    ForEach(Buttons.Action.allCases) { Text($0.label).tag($0) }
                }
                DisclosureGroup("Buzzes") {
                    Text("Keep: one short. End: one medium. Pause: one long; resume: two short. Mute: two long; unmute: three short. Holding the button 3 s turns the pendant off.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                if pendant.led != nil {
                    Slider(value: $led, in: 0...100, step: 5) {
                        Text("Light")
                    } minimumValueLabel: {
                        Image(systemName: "lightbulb.min").foregroundStyle(.secondary)
                    } maximumValueLabel: {
                        Image(systemName: "lightbulb.max").foregroundStyle(.secondary)
                    } onEditingChanged: { editing in
                        if !editing { pendant.setLED(Int(led)) }
                    }
                    .onAppear { led = Double(pendant.led ?? 50) }
                    .onChange(of: pendant.led) { _, level in if let level { led = Double(level) } }
                }
            } header: {
                Text("Pendant")
            }

            Section {
                Toggle("Keep paused audio on this phone", isOn: Binding(get: { pause.keep },
                                                                        set: { pause.setKeep($0) }))
            } header: {
                Text("Pausing")
            } footer: {
                Text("Kept audio stays on this phone for 30 days, for you to review. Otherwise paused audio is dropped as it arrives.")
            }

            Section {
                LabeledContent("Now", value: location.state.label)
                LabeledContent("Car", value: Settings.carName.isEmpty ? "Not set" : Settings.carName)
                if let output = location.output, output != Settings.carName {
                    Button("Use \u{201C}\(output)\u{201D} as my car") { location.useAsCar() }
                }
            } header: {
                Text("Location")
            } footer: {
                Text("While connected to your car's Bluetooth, tap to use it as your car. Connecting and disconnecting help tell when a trip starts and ends.")
            }

            Section {
                Group {
                    LabeledContent("Capture URL") {
                        TextField("Capture", text: $url, prompt: Text("http://<nas>:8789/capture"))
                            .keyboardType(.URL)
                    }
                    LabeledContent("Token") {
                        SecureField("Token", text: $token,
                                    prompt: Text(Settings.token == nil ? "Required" : "Saved; enter to replace"))
                    }
                    LabeledContent("Portal URL") {
                        TextField("Portal", text: $portal, prompt: Text("http://<nas>:8788"))
                            .keyboardType(.URL)
                    }
                }
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .multilineTextAlignment(.trailing)
                Button("Save") {
                    Settings.save(url: url, token: token)
                    Settings.save(portal: portal)
                    token = ""
                    uploader.drain()
                }
                .fontWeight(.semibold)
                .disabled(url == Settings.urlText && portal == Settings.portalText && token.isEmpty)
            } header: {
                Text("Server")
            } footer: {
                Text("install/ios.sh prints the capture URL, token and portal URL. The phone must be on the tailnet.")
            }

            Section {
                Button("Use a Different Pendant", role: .destructive) { confirmForget = true }
                    .confirmationDialog("Forget this pendant and connect to the next one found?",
                                        isPresented: $confirmForget, titleVisibility: .visible) {
                        Button("Forget", role: .destructive) { pendant.forget() }
                    }
            }
        }
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.inline)
    }
}
