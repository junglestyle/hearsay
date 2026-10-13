import AVFoundation
import CoreLocation
import CoreMotion
import Foundation

/// Where the owner is while recording, for hearsay/places.py, spending as
/// little battery as it can: Movement decides when a fix is worth it, and this
/// carries that out and records what it saw. Only while recording (neither
/// paused nor muted); everything stops on a pause.
///
/// Records in the spool, all raw observations kept on the NAS:
/// - LOCATION: each fix (Spool.location).
/// - ACTIVITY: each Core Motion activity update (Spool.activity). The motion
///   coprocessor classifies on its own, so this costs next to nothing.
/// - ROUTE: the phone's Bluetooth and car audio outputs whenever they change
///   (Spool.route). Read from the audio session's current route, never by
///   changing it: the pendant's audio doesn't use the phone's session at all.
///   The car is a CarPlay output, or the Bluetooth output the owner picked.
///   A car only shows while it's the output, so it can miss one; it's a hint.
/// - MOVEMENT: Movement's states and heartbeats (Spool.movement).
///
/// A fix runs Core Location for up to a precise fix's 45 s (GPS can take that
/// long), and stops early once one is good enough. Settled, a geofence
/// (CLMonitor, cell and Wi-Fi, no GPS) watches for leaving.
///
/// Not used: AirTags. Find My has no public API, and an AirTag isn't a beacon
/// an app can range.
final class Location: NSObject, ObservableObject, CLLocationManagerDelegate {
    private static let preciseTarget = 20.0
    private static let preciseTimeout: TimeInterval = 45
    private static let roughTarget = 100.0
    private static let roughTimeout: TimeInterval = 15
    private static let anchorID = "anchor"

    /// The Bluetooth or car audio output right now, to offer as the car.
    @Published private(set) var output: String?
    @Published private(set) var state = Movement.State.off

    private let spool: Spool
    private let manager = CLLocationManager()
    private let motion = CMMotionActivityManager()
    private var movement = Movement()
    private var monitor: CLMonitor?
    private var recording = false
    private var session: (started: Date, precise: Bool, best: CLLocation?)?
    private var lastRoute: String?

    init(spool: Spool) {
        self.spool = spool
        super.init()
        manager.delegate = self
        // The app records in the background (the pendant keeps it running),
        // so fixes must work there too: Always, and background updates.
        manager.allowsBackgroundLocationUpdates = true
        manager.showsBackgroundLocationIndicator = false
        manager.pausesLocationUpdatesAutomatically = false
        NotificationCenter.default.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil,
                                               queue: .main) { [weak self] _ in self?.routeChanged() }
        Task { @MainActor in
            let monitor = await CLMonitor("hearsay")
            self.monitor = monitor
            await monitor.remove(Self.anchorID)  // left from before a relaunch: Movement starts fresh
            for try await event in await monitor.events where event.state == .unsatisfied {
                self.apply(self.movement.leftAnchor(at: Date().timeIntervalSince1970))
            }
        }
        routeChanged()
    }

    /// Called every second with whether the app is recording right now.
    func tick(recording now: Bool) {
        let at = Date().timeIntervalSince1970
        if now != recording {
            recording = now
            if now { start() } else { stop() }
            apply(movement.recording(now, at: at))
            if now {
                lastRoute = nil  // Movement starts afresh: tell it about the car again
                routeChanged()
            }
        }
        guard now else { return }
        apply(movement.tick(at: at))
        if let session, Date().timeIntervalSince(session.started) >= (session.precise ? Self.preciseTimeout : Self.roughTimeout) {
            finishFix()
        }
    }

    func useAsCar() {
        Settings.carName = output ?? ""
        routeChanged()
    }

    private func start() {
        switch manager.authorizationStatus {
        case .notDetermined: manager.requestAlwaysAuthorization()
        default: break  // Denied in Settings: recording goes on without places.
        }
        guard CMMotionActivityManager.isActivityAvailable() else { return }
        motion.startActivityUpdates(to: .main) { [weak self] activity in
            guard let self, let activity else { return }
            self.record(activity)
            self.apply(self.movement.activity(Self.kind(activity), Self.confidence(activity.confidence),
                                              at: Date().timeIntervalSince1970))
        }
    }

    private func stop() {
        motion.stopActivityUpdates()
        if session != nil {
            manager.stopUpdatingLocation()
            session = nil
        }
    }

    private func apply(_ effects: [Movement.Effect]) {
        for effect in effects {
            switch effect {
            case let .requestFix(precise):
                requestFix(precise: precise)
            case let .watch(anchor):
                let center = CLLocationCoordinate2D(latitude: anchor.latitude, longitude: anchor.longitude)
                Task { await monitor?.add(CLMonitor.CircularGeographicCondition(center: center, radius: anchor.radius),
                                          identifier: Self.anchorID, assuming: .satisfied) }
            case .unwatch:
                Task { await monitor?.remove(Self.anchorID) }
            case let .record(state, confidence, reason, anchor):
                self.state = state
                record(state, confidence: confidence, reason: reason, anchor: anchor)
            }
        }
    }

    private func requestFix(precise: Bool) {
        guard session == nil, [.authorizedAlways, .authorizedWhenInUse].contains(manager.authorizationStatus) else { return }
        manager.desiredAccuracy = precise ? kCLLocationAccuracyBest : kCLLocationAccuracyHundredMeters
        session = (Date(), precise, nil)
        manager.startUpdatingLocation()
    }

    /// Stops Core Location and hands over the best fix the session saw, if any.
    private func finishFix() {
        guard let finished = session else { return }
        manager.stopUpdatingLocation()
        session = nil
        guard let best = finished.best else { return }
        var data = Data()
        withUnsafeBytes(of: best.coordinate.latitude.bitPattern.littleEndian) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: best.coordinate.longitude.bitPattern.littleEndian) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: Float(best.horizontalAccuracy).bitPattern.littleEndian) { data.append(contentsOf: $0) }
        spool.append(kind: Spool.location, data: data)
        apply(movement.fix(Movement.Fix(at: best.timestamp.timeIntervalSince1970, latitude: best.coordinate.latitude,
                                        longitude: best.coordinate.longitude, accuracy: best.horizontalAccuracy)))
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard var current = session else { return }
        // A cached fix from before this session says nothing new.
        for fix in locations where fix.horizontalAccuracy >= 0 && fix.timestamp >= current.started.addingTimeInterval(-5) {
            if current.best.map({ fix.horizontalAccuracy < $0.horizontalAccuracy }) ?? true { current.best = fix }
        }
        session = current
        if let best = current.best, best.horizontalAccuracy <= (current.precise ? Self.preciseTarget : Self.roughTarget) {
            finishFix()
        }
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        log.error("location failed: \(error)")
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        log.info("location authorization: \(manager.authorizationStatus.rawValue)")
        // When In Use first; iOS asks the owner about Always later, on its own schedule.
        if manager.authorizationStatus == .authorizedWhenInUse {
            manager.requestAlwaysAuthorization()
        }
    }

    // MARK: The car

    private func routeChanged() {
        let kinds: [AVAudioSession.Port] = [.carAudio, .bluetoothA2DP, .bluetoothHFP, .bluetoothLE]
        let bluetooth = AVAudioSession.sharedInstance().currentRoute.outputs.filter { kinds.contains($0.portType) }
        output = bluetooth.first?.portName
        let car = bluetooth.contains { $0.portType == .carAudio || (!Settings.carName.isEmpty && $0.portName == Settings.carName) }
        let described = bluetooth.map { "\($0.portType.rawValue)\t\($0.portName)" }.joined(separator: "\n")
        guard recording, described + "\(car)" != lastRoute else { return }
        lastRoute = described + "\(car)"
        spool.append(kind: Spool.route, data: Data([car ? 1 : 0]) + Data(described.utf8))
        apply(movement.car(connected: car, at: Date().timeIntervalSince1970))
    }

    // MARK: Records

    private func record(_ activity: CMMotionActivity) {
        var flags: UInt8 = 0
        for (bit, on) in [activity.stationary, activity.walking, activity.running, activity.cycling,
                          activity.automotive, activity.unknown].enumerated() where on {
            flags |= 1 << bit
        }
        var data = Data()
        withUnsafeBytes(of: activity.startDate.timeIntervalSince1970.bitPattern.littleEndian) { data.append(contentsOf: $0) }
        data.append(flags)
        data.append(UInt8(activity.confidence.rawValue))
        spool.append(kind: Spool.activity, data: data)
    }

    private func record(_ state: Movement.State, confidence: Double, reason: Movement.Reason, anchor: Movement.Anchor?) {
        var data = Data([state.rawValue, UInt8((confidence * 100).rounded()), reason.rawValue])
        if let anchor {
            withUnsafeBytes(of: anchor.latitude.bitPattern.littleEndian) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: anchor.longitude.bitPattern.littleEndian) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: Float(anchor.radius).bitPattern.littleEndian) { data.append(contentsOf: $0) }
        }
        spool.append(kind: Spool.movement, data: data)
    }

    /// Driving wins over the rest, as Core Motion can say several at once
    /// (stationary and automotive, at a red light).
    private static func kind(_ activity: CMMotionActivity) -> Movement.Activity {
        if activity.automotive { return .automotive }
        if activity.cycling { return .cycling }
        if activity.walking || activity.running { return .walking }
        if activity.stationary { return .stationary }
        return .unknown
    }

    private static func confidence(_ confidence: CMMotionActivityConfidence) -> Movement.Confidence {
        switch confidence {
        case .high: .high
        case .medium: .medium
        default: .low
        }
    }
}
