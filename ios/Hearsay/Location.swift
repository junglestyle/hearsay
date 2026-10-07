import CoreLocation
import Foundation

/// Where the owner is while recording, for hearsay/places.py: one reading when
/// recording starts (neither paused nor muted), then one every 2 minutes while it
/// lasts. About 100 m accuracy, which Wi-Fi and cell towers give without GPS,
/// so it's cheap on battery. Each reading is a LOCATION record in the spool:
/// latitude f64 LE, longitude f64 LE, then the accuracy iOS reports f32 LE in
/// metres. The NAS reduces readings to places the operator named; coordinates
/// never reach the stream.
final class Location: NSObject, CLLocationManagerDelegate {
    private static let interval: TimeInterval = 120

    private let spool: Spool
    private let manager = CLLocationManager()
    private var recording = false
    private var lastAsked: Date?

    init(spool: Spool) {
        self.spool = spool
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        // The app records in the background (the pendant keeps it running),
        // so readings must work there too: Always, and background updates.
        manager.allowsBackgroundLocationUpdates = true
        manager.showsBackgroundLocationIndicator = false
    }

    /// Called every second with whether the app is recording right now.
    func tick(recording now: Bool) {
        defer { recording = now }
        guard now else { return }
        let started = !recording
        guard started || lastAsked.map({ Date().timeIntervalSince($0) >= Self.interval }) ?? true else { return }
        lastAsked = Date()
        switch manager.authorizationStatus {
        case .notDetermined:
            manager.requestAlwaysAuthorization()
        case .authorizedAlways, .authorizedWhenInUse:
            manager.requestLocation()
        default:
            break  // Denied in Settings: recording goes on without places.
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let fix = locations.last, fix.horizontalAccuracy >= 0 else { return }
        var data = Data()
        withUnsafeBytes(of: fix.coordinate.latitude.bitPattern.littleEndian) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: fix.coordinate.longitude.bitPattern.littleEndian) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: Float(fix.horizontalAccuracy).bitPattern.littleEndian) { data.append(contentsOf: $0) }
        spool.append(kind: Spool.location, data: data)
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        log.error("location reading failed: \(error)")
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        log.info("location authorization: \(manager.authorizationStatus.rawValue)")
        // When In Use first; iOS asks the owner about Always later, on its own schedule.
        if manager.authorizationStatus == .authorizedWhenInUse {
            manager.requestAlwaysAuthorization()
        }
    }
}
