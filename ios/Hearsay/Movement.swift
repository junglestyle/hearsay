import Foundation

/// Whether the owner is staying put or on the move, from timestamped
/// observations: location fixes, Core Motion's activity, whether the car's
/// audio is connected, and the geofence around where they settled. Plain
/// logic with no Apple frameworks, so it is tested on its own
/// (ios/Tests/MovementTests.swift); Location feeds it and carries out what it
/// asks for.
///
/// While moving, a precise fix about every movingInterval. Settling takes two
/// usable fixes at least minSpan apart that agree within their accuracy, with
/// no confident walking, cycling or driving between them: that makes an
/// anchor, watched by a geofence, and fixes stop. Walking only delays it:
/// precise fixes that keep agreeing for lingerSpan settle anyway. Core Motion saying
/// stationary for settleDwell asks for those fixes sooner. Right after
/// driving it takes vehicleDwell, so a red light or a queue doesn't settle,
/// until the owner walks or the car disconnects (an arrival). Leaving is only suspected (the geofence,
/// sustained motion, the car connecting, a cheap fix every checkInterval)
/// until a precise fix lands outside the anchor by more than its own
/// accuracy, and a single one only counts with motion or the geofence
/// agreeing; otherwise a second fix confirmDelay later must say so too.
/// The car is context, never proof: connected says it's near, not moving;
/// disconnecting asks for a fix, as a possible arrival.
///
/// Every transition, and a heartbeat every heartbeat seconds while settled,
/// is recorded with its confidence and reason, so the NAS can tell where
/// audio was recorded during a long stay with no fresh fixes, and trips can
/// be reconstructed later from raw.
struct Movement {
    enum State: UInt8 { case off = 0, uncertain, stationary, walking, cycling, driving }
    enum Activity { case stationary, walking, cycling, automotive, unknown }
    enum Confidence: Int, Comparable {
        case low, medium, high
        static func < (a: Confidence, b: Confidence) -> Bool { a.rawValue < b.rawValue }
    }
    /// Why a state was recorded. Raw values are the upload format (Spool.movement).
    enum Reason: UInt8 { case heartbeat = 0, started, stopped, settled, departed, activity, resumed }

    struct Fix: Equatable {
        let at: Double
        let latitude: Double
        let longitude: Double
        let accuracy: Double  // metres, as iOS reports it; negative is invalid
    }

    struct Anchor: Equatable {
        let latitude: Double
        let longitude: Double
        let radius: Double  // metres
    }

    enum Effect: Equatable {
        case requestFix(precise: Bool)
        case watch(Anchor)
        case unwatch
        case record(State, confidence: Double, reason: Reason, anchor: Anchor?)
    }

    static let movingInterval = 300.0
    static let settleDwell = 180.0
    static let vehicleDwell = 600.0
    static let minSpan = 120.0
    /// Fixes agreeing this long settle even while walking: going nowhere.
    static let lingerSpan = 600.0
    static let confirmDelay = 60.0
    static let checkInterval = 1800.0
    static let heartbeat = 300.0
    /// Motion this long while settled suspects leaving...
    static let sustained = 60.0
    /// ...at most this often, doubling each time it was nothing, up to checkInterval.
    static let suspicionGap = 300.0
    /// A fix asked for that never came (no signal) can be asked for again after this.
    static let fixTimeout = 90.0
    /// Fixes less accurate than this are recorded but not reasoned on.
    static let usableAccuracy = 200.0
    /// A single displaced fix this accurate departs when motion or the geofence agree.
    static let goodAccuracy = 50.0
    static let minRadius = 100.0

    private(set) var state = State.off
    private(set) var anchor: Anchor?
    /// Where the owner was settled when recording paused, to resume there.
    private var pausedAt: Anchor?
    private var fixes: [Fix] = []
    private var activity = (kind: Activity.unknown, confidence: Confidence.low, since: 0.0)
    private var lastMoving = -Double.infinity  // when motion last said walking, cycling or driving
    private var car = false
    /// Driving was the last motion, and the car hasn't disconnected since.
    private var droveLast = false
    private var anchorConfidence = 0.0
    private var asking: Double?  // when the fix in flight was asked for
    private var lastAsked = -Double.infinity
    private var displaced: Fix?
    private var leftAt = -Double.infinity  // the geofence said we left
    private var lastHeartbeat = 0.0
    private var lastSuspicion = -Double.infinity
    private var suspicionWait = suspicionGap
    private var arrivalCheck: Double?

    mutating func recording(_ on: Bool, at now: Double) -> [Effect] {
        if on == (state != .off) { return [] }
        if !on {
            pausedAt = anchor
            let effects: [Effect] = (anchor == nil ? [] : [.unwatch]) + [.record(.off, confidence: 1, reason: .stopped, anchor: nil)]
            self = Movement(pausedAt: pausedAt)
            return effects
        }
        state = .uncertain
        return [.record(.uncertain, confidence: 0.2, reason: .started, anchor: nil)] + ask(precise: true, at: now)
    }

    private init(pausedAt: Anchor?) { self.pausedAt = pausedAt }
    init() {}

    mutating func fix(_ fix: Fix) -> [Effect] {
        guard state != .off else { return [] }
        asking = nil
        guard fix.accuracy >= 0, fix.accuracy <= Self.usableAccuracy else { return [] }
        if let anchor { return checkDeparture(fix, from: anchor) }
        if state == .uncertain, let paused = pausedAt, fixes.isEmpty, !movingNow(at: fix.at),
           distance(fix, paused) <= paused.radius, fix.accuracy <= paused.radius {
            return settle(at: paused, confidence: 0.7, reason: .resumed, now: fix.at)
        }
        pausedAt = nil
        fixes = fixes.filter { fix.at - $0.at <= Self.vehicleDwell + Self.movingInterval } + [fix]
        let dwell = droveLast ? Self.vehicleDwell : Self.minSpan
        if let earlier = fixes.dropLast().last(where: { fix.at - $0.at >= dwell && agree($0, fix) }),
           lastMoving < earlier.at, !movingNow(at: fix.at) {
            let still = activity.kind == .stationary && activity.confidence >= .medium
            return settle(at: center(of: fixes.filter { $0.at >= earlier.at && agree($0, fix) }),
                          confidence: still ? 0.9 : 0.7, reason: .settled, now: fix.at)
        }
        // Moving about one spot (a party, a dance class) is staying there: where
        // the fixes are outweighs the walking. Not after driving: a jam isn't a stay.
        if !droveLast, let earlier = fixes.dropLast().last(where: { fix.at - $0.at >= Self.lingerSpan }) {
            let used = fixes.filter { $0.at >= earlier.at }
            if used.count >= 3, used.allSatisfy({ $0.accuracy <= Self.goodAccuracy && agree($0, fix) }) {
                return settle(at: center(of: used), confidence: 0.5, reason: .settled, now: fix.at)
            }
        }
        return classify(at: fix.at)
    }

    /// Weighted towards the sharper fixes, wide enough for the worst of them.
    private func center(of used: [Fix]) -> Anchor {
        let weights = used.map { 1 / max($0.accuracy, 5) / max($0.accuracy, 5) }
        let total = weights.reduce(0, +)
        return Anchor(
            latitude: zip(used, weights).map { $0.latitude * $1 }.reduce(0, +) / total,
            longitude: zip(used, weights).map { $0.longitude * $1 }.reduce(0, +) / total,
            radius: max(Self.minRadius, 1.5 * used.map(\.accuracy).max()!))
    }

    mutating func activity(_ kind: Activity, _ confidence: Confidence, at now: Double) -> [Effect] {
        if kind != activity.kind || confidence != activity.confidence {
            activity = (kind, confidence, kind == activity.kind ? activity.since : now)
        }
        if moving(kind, confidence) {
            lastMoving = now
            droveLast = kind == .automotive
        }
        guard state != .off, anchor == nil else { return [] }
        return classify(at: now)
    }

    mutating func car(connected: Bool, at now: Double) -> [Effect] {
        car = connected
        guard state != .off else { return [] }
        if anchor != nil, connected {
            return ask(precise: true, at: now)  // getting in, maybe: see whether we leave
        }
        if anchor == nil, !connected {
            // Possibly arrived: a fix now, and another once it could agree with it.
            droveLast = false
            arrivalCheck = now + Self.minSpan
            return ask(precise: true, at: now) + classify(at: now)
        }
        return anchor == nil ? classify(at: now) : []
    }

    mutating func leftAnchor(at now: Double) -> [Effect] {
        guard anchor != nil else { return [] }
        leftAt = now
        return ask(precise: true, at: now)
    }

    mutating func tick(at now: Double) -> [Effect] {
        guard state != .off else { return [] }
        if let asked = asking, now - asked >= Self.fixTimeout { asking = nil }
        if let anchor {
            var effects: [Effect] = []
            if now - lastHeartbeat >= Self.heartbeat {
                lastHeartbeat = now
                effects.append(.record(.stationary, confidence: anchorConfidence, reason: .heartbeat, anchor: anchor))
            }
            if let displaced, now - displaced.at >= Self.confirmDelay {
                return effects + ask(precise: true, at: now)
            }
            if moving(activity.kind, activity.confidence), now - activity.since >= Self.sustained,
               now - lastSuspicion >= suspicionWait {
                if lastSuspicion.isFinite { suspicionWait = min(suspicionWait * 2, Self.checkInterval) }
                lastSuspicion = now
                return effects + ask(precise: true, at: now)
            }
            if now - lastAsked >= Self.checkInterval {
                return effects + ask(precise: false, at: now)
            }
            return effects
        }
        if let check = arrivalCheck, now >= check {
            arrivalCheck = nil
            return ask(precise: true, at: now)
        }
        let dwell = droveLast ? Self.vehicleDwell : Self.settleDwell
        let still = activity.kind == .stationary && activity.confidence >= .medium
        if still, now - activity.since >= dwell, now - lastAsked >= Self.minSpan {
            return ask(precise: true, at: now)
        }
        if now - lastAsked >= Self.movingInterval {
            return ask(precise: true, at: now)
        }
        return []
    }

    private mutating func checkDeparture(_ fix: Fix, from anchor: Anchor) -> [Effect] {
        guard distance(fix, anchor) - fix.accuracy > anchor.radius else {
            displaced = nil  // an outlier, or a check that found us home
            return []
        }
        let corroborated = fix.accuracy <= Self.goodAccuracy && (movingNow(at: fix.at) || fix.at - leftAt <= Self.movingInterval)
        guard displaced != nil || corroborated else {
            displaced = fix
            return []
        }
        self.anchor = nil
        displaced = nil
        fixes = [fix]
        suspicionWait = Self.suspicionGap
        let (next, confidence) = movingState()
        state = next
        return [.unwatch, .record(next, confidence: confidence, reason: .departed, anchor: nil)]
    }

    private mutating func settle(at anchor: Anchor, confidence: Double, reason: Reason, now: Double) -> [Effect] {
        self.anchor = anchor
        anchorConfidence = confidence
        state = .stationary
        fixes = []
        pausedAt = nil
        displaced = nil
        arrivalCheck = nil
        lastHeartbeat = now
        lastSuspicion = -.infinity  // leaving right after settling is still noticed
        suspicionWait = Self.suspicionGap
        return [.watch(anchor), .record(.stationary, confidence: confidence, reason: reason, anchor: anchor)]
    }

    /// The moving state motion and the car suggest, recorded when it changes.
    private mutating func classify(at now: Double) -> [Effect] {
        let (next, confidence) = movingState()
        guard next != state else { return [] }
        state = next
        return [.record(next, confidence: confidence, reason: .activity, anchor: nil)]
    }

    private func movingState() -> (State, Double) {
        let level = [0.3, 0.6, 0.9][activity.confidence.rawValue]
        if activity.confidence >= .medium {
            switch activity.kind {
            case .automotive: return (.driving, level)
            case .cycling: return (.cycling, level)
            case .walking: return (.walking, level)
            default: break
            }
        }
        // Connected to the car and not plainly on foot: probably in it.
        if car, !(activity.kind == .walking && activity.confidence >= .medium) { return (.driving, 0.4) }
        return (.uncertain, 0.2)
    }

    private mutating func ask(precise: Bool, at now: Double) -> [Effect] {
        guard asking == nil else { return [] }
        asking = now
        lastAsked = now
        return [.requestFix(precise: precise)]
    }

    private func moving(_ kind: Activity, _ confidence: Confidence) -> Bool {
        confidence >= .medium && [.walking, .cycling, .automotive].contains(kind)
    }

    private func movingNow(at now: Double) -> Bool {
        moving(activity.kind, activity.confidence) && now - lastMoving <= Self.movingInterval
    }

    /// Two fixes that could be the same spot, given how sure of each iOS is.
    private func agree(_ a: Fix, _ b: Fix) -> Bool {
        distance(a, b) <= max(50, a.accuracy, b.accuracy)
    }
}

/// Metres over the earth's surface (haversine).
func distance(_ a: Movement.Fix, _ b: Movement.Fix) -> Double {
    metres(a.latitude, a.longitude, b.latitude, b.longitude)
}

func distance(_ a: Movement.Fix, _ b: Movement.Anchor) -> Double {
    metres(a.latitude, a.longitude, b.latitude, b.longitude)
}

func metres(_ lat1: Double, _ lon1: Double, _ lat2: Double, _ lon2: Double) -> Double {
    let p1 = lat1 * .pi / 180, p2 = lat2 * .pi / 180
    let dp = p2 - p1, dl = (lon2 - lon1) * .pi / 180
    let h = sin(dp / 2) * sin(dp / 2) + cos(p1) * cos(p2) * sin(dl / 2) * sin(dl / 2)
    return 2 * 6_371_000 * asin(min(1, sqrt(h)))
}
