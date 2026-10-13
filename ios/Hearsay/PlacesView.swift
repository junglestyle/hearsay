import MapKit
import SwiftUI

/// The places the operator has named, on Apple's map, so places go to no one
/// new: drag a named spot's pin to where the place really is, or name a spot
/// where the owner spent time. The portal (hearsay/portal.py) serves the
/// spots and keeps the names; its Places page stays the way to hear a spot.
struct PlacesView: View {
    struct Named: Identifiable {
        let id: Int
        let name: String
        let coordinate: CLLocationCoordinate2D
    }

    struct Unnamed: Identifiable {
        let id: Int
        // As the portal listed them: ignoring a spot matches them exactly.
        let latitude: Double
        let longitude: Double
        let minutes: Int
        var coordinate: CLLocationCoordinate2D { .init(latitude: latitude, longitude: longitude) }
    }

    @State private var named: [Named] = []
    @State private var unnamed: [Unnamed] = []
    @State private var moved: [Named.ID: CLLocationCoordinate2D] = [:]
    @State private var grab: (id: Named.ID, offset: CGSize)?
    @State private var naming: Unnamed?
    @State private var camera = MapCameraPosition.automatic
    @State private var loaded = false
    @State private var busy = false
    @State private var failure: String?

    var body: some View {
        NavigationStack {
            Group {
                if Settings.portalURL == nil || Settings.token == nil {
                    ContentUnavailableView("No Portal Address", systemImage: "map",
                                           description: Text("Enter the portal URL and token in the Recorder tab's Settings, under Server."))
                } else {
                    map
                }
            }
            .navigationTitle("Places")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { toolbar }
        }
        .task { if !loaded { await load() } }
        .sheet(item: $naming) { spot in
            NameSpot(spot: spot, names: Array(Set(named.map(\.name))).sorted()) { fields in
                naming = nil
                Task { await send(fields) }
            }
        }
        .alert("Places", isPresented: Binding(get: { failure != nil }, set: { if !$0 { failure = nil } })) {
            Button("OK") {}
        } message: {
            Text(failure ?? "")
        }
    }

    private var map: some View {
        MapReader { proxy in
            Map(position: $camera) {
                UserAnnotation()
                ForEach(unnamed) { spot in
                    Annotation("\(spot.minutes) min", coordinate: spot.coordinate) {
                        Circle()
                            .strokeBorder(Color.ink, style: StrokeStyle(lineWidth: 2, dash: [3, 2]))
                            .background(Circle().fill(.background))
                            .frame(width: 22, height: 22)
                            .contentShape(.circle)
                            .onTapGesture { naming = spot }
                            .accessibilityLabel("Spot to name, \(spot.minutes) minutes here")
                            .accessibilityAddTraits(.isButton)
                    }
                }
                ForEach(named) { spot in
                    Annotation(spot.name, coordinate: moved[spot.id] ?? spot.coordinate, anchor: .bottom) {
                        pin(dragging: grab?.id == spot.id, moved: moved[spot.id] != nil)
                            .gesture(drag(spot, proxy))
                            .accessibilityLabel(spot.name)
                            .accessibilityHint("Drag to where this place really is")
                    }
                }
            }
            .mapStyle(.standard(emphasis: .muted))
            .mapControls {
                MapUserLocationButton()
                MapCompass()
            }
            .sensoryFeedback(.selection, trigger: grab?.id)
        }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        if !moved.isEmpty {
            ToolbarItem(placement: .cancellationAction) {
                Button("Revert") { withAnimation(.snappy) { moved = [:] } }
                    .disabled(busy)
            }
            ToolbarItem(placement: .confirmationAction) {
                Button("Save") { Task { await save() } }
                    .fontWeight(.semibold)
                    .disabled(busy)
            }
        } else if Settings.portalURL != nil {
            ToolbarItem(placement: .primaryAction) {
                if busy {
                    ProgressView()
                } else {
                    Button { Task { await load() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                }
            }
        }
    }

    /// A filled ink pin, lifted while it's dragged; hollow once moved and not yet saved.
    private func pin(dragging: Bool, moved: Bool) -> some View {
        VStack(spacing: 0) {
            Circle()
                .fill(moved ? Color(.systemBackground) : Color.ink)
                .strokeBorder(Color.ink, lineWidth: 3)
                .frame(width: 20, height: 20)
            Rectangle()
                .fill(Color.ink)
                .frame(width: 2, height: 10)
        }
        .scaleEffect(dragging ? 1.3 : 1, anchor: .bottom)
        .offset(y: dragging ? -6 : 0)
        .animation(.snappy, value: dragging)
        .padding(10)  // a bigger target than the pin itself
        .contentShape(.rect)
        .padding(-10)
    }

    /// Keeps the point of the pin where it was relative to the finger, so it
    /// doesn't jump to it.
    private func drag(_ spot: Named, _ proxy: MapProxy) -> some Gesture {
        DragGesture(minimumDistance: 2, coordinateSpace: .global)
            .onChanged { value in
                if grab?.id != spot.id {
                    let tip = proxy.convert(moved[spot.id] ?? spot.coordinate, to: .global) ?? value.startLocation
                    grab = (spot.id, CGSize(width: tip.x - value.startLocation.x, height: tip.y - value.startLocation.y))
                }
                let offset = grab?.offset ?? .zero
                let point = CGPoint(x: value.location.x + offset.width, y: value.location.y + offset.height)
                if let coordinate = proxy.convert(point, from: .global) { moved[spot.id] = coordinate }
            }
            .onEnded { _ in grab = nil }
    }

    private func load() async {
        busy = true
        defer { busy = false }
        do {
            let (data, response) = try await Self.session.data(for: request("places.json"))
            try check(response)
            let listing = try JSONDecoder().decode(Listing.self, from: data)
            named = listing.named.enumerated().map {
                Named(id: $0.offset, name: $0.element.name,
                      coordinate: .init(latitude: $0.element.latitude, longitude: $0.element.longitude))
            }
            unnamed = listing.unnamed.enumerated().map {
                Unnamed(id: $0.offset, latitude: $0.element.latitude, longitude: $0.element.longitude,
                        minutes: $0.element.minutes)
            }
            moved = [:]
            loaded = true
        } catch {
            failure = describe(error)
        }
    }

    /// One move per place with a dragged spot, listing all of its spots.
    private func save() async {
        let names = Set(named.filter { moved[$0.id] != nil }.map(\.name))
        for name in names.sorted() {
            let spots = named.filter { $0.name == name }.map { moved[$0.id] ?? $0.coordinate }
            let at = spots.map { ("at", String(format: "%.6f,%.6f", $0.latitude, $0.longitude)) }
            guard await post([("action", "move"), ("from", name)] + at) else { return }
        }
        await load()
    }

    private func send(_ fields: [(String, String)]) async {
        if await post(fields) { await load() }
    }

    /// The portal answers a form with a redirect: back to /places when it
    /// took it, to /login when it refused the token.
    private func post(_ fields: [(String, String)]) async -> Bool {
        busy = true
        defer { busy = false }
        do {
            var request = try request("places")
            request.httpMethod = "POST"
            request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
            let allowed = CharacterSet.alphanumerics.union(.init(charactersIn: "-._~"))
            request.httpBody = fields
                .map { "\($0.0)=\($0.1.addingPercentEncoding(withAllowedCharacters: allowed) ?? "")" }
                .joined(separator: "&")
                .data(using: .utf8)
            let (_, response) = try await Self.session.data(for: request)
            let location = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Location") ?? ""
            if location.hasSuffix("/login") { throw Failure.token }
            try check(response, allowing: 303)
            return true
        } catch {
            failure = describe(error)
            return false
        }
    }

    private func request(_ path: String) throws -> URLRequest {
        guard let portal = Settings.portalURL, let token = Settings.token else { throw Failure.settings }
        var request = URLRequest(url: portal.appendingPathComponent(path), timeoutInterval: 20)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return request
    }

    private func check(_ response: URLResponse, allowing allowed: Int = 200) throws {
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 { throw Failure.token }
        if status != allowed { throw Failure.status(status) }
    }

    private func describe(_ error: Error) -> String {
        switch error {
        case Failure.settings: "Enter the portal URL and token in Settings."
        case Failure.token: "The portal refused the capture token. Check it in Settings."
        case Failure.status(let status): "The portal answered HTTP \(status)."
        default: "Can't reach the portal: \(error.localizedDescription). Is Tailscale on?"
        }
    }

    private enum Failure: Error {
        case settings, token, status(Int)
    }

    private struct Listing: Decodable {
        struct Spot: Decodable {
            let name: String
            let latitude: Double
            let longitude: Double
        }

        struct Candidate: Decodable {
            let latitude: Double
            let longitude: Double
            let minutes: Int
        }

        let named: [Spot]
        let unnamed: [Candidate]
    }

    /// Doesn't follow redirects, so a form's answer can be read.
    private final class NoRedirects: NSObject, URLSessionTaskDelegate {
        func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                        newRequest request: URLRequest) async -> URLRequest? {
            nil
        }
    }

    private static let session = URLSession(configuration: .ephemeral, delegate: NoRedirects(), delegateQueue: nil)
}

/// Name a spot, give it to a place already named (another of its spots), or
/// say it isn't a place.
private struct NameSpot: View {
    let spot: PlacesView.Unnamed
    let names: [String]
    let send: ([(String, String)]) -> Void
    @State private var name = ""
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Name", text: $name)
                        .focused($focused)
                        .submitLabel(.done)
                        .onSubmit(save)
                    if !names.isEmpty {
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 8) {
                                ForEach(names, id: \.self) { existing in
                                    Button(existing) { name = existing }
                                        .buttonStyle(.bordered)
                                        .controlSize(.small)
                                }
                            }
                        }
                        .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
                    }
                } footer: {
                    Text("\(spot.minutes) minutes here. Reusing a name adds this spot to that place. The portal's Places page has clips heard here.")
                }
                Section {
                    Button("Not a Place", role: .destructive) {
                        send([("action", "ignore"), ("latitude", "\(spot.latitude)"), ("longitude", "\(spot.longitude)")])
                    }
                }
            }
            .navigationTitle("Name This Spot")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save", action: save)
                        .fontWeight(.semibold)
                        .disabled(trimmed.isEmpty)
                }
            }
        }
        .presentationDetents([.medium])
        .onAppear { focused = true }
    }

    private var trimmed: String { name.trimmingCharacters(in: .whitespacesAndNewlines) }

    private func save() {
        guard !trimmed.isEmpty else { return }
        send([("action", "name"), ("name", trimmed), ("latitude", "\(spot.latitude)"), ("longitude", "\(spot.longitude)")])
    }
}
