import Foundation
import UIKit

/// Sends sealed uploads to the capture receiver on the NAS, oldest first, one
/// at a time. The file name is the idempotency key, so an upload that reached
/// the NAS but whose answer was lost is recognized as a duplicate when resent.
///
/// Runs whenever the app does. In the background that's while the pendant is
/// sending audio (each notification wakes the app), and each upload asks iOS
/// for time to finish. Anything left waits for the next wake.
final class Uploader: ObservableObject {
    @Published private(set) var pending = 0
    @Published private(set) var lastResult = "nothing sent yet"

    private let spool: Spool
    private var busy = false

    init(spool: Spool) {
        self.spool = spool
        pending = spool.pending().count
    }

    func drain() {
        let files = spool.pending()
        pending = files.count
        guard !busy, let file = files.first else { return }
        guard let url = Settings.url, let token = Settings.token else {
            lastResult = "not sent: set the server URL and token"
            return
        }
        busy = true
        var request = URLRequest(url: url, timeoutInterval: 60)
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(file.deletingPathExtension().lastPathComponent, forHTTPHeaderField: "Idempotency-Key")
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        var background = UIBackgroundTaskIdentifier.invalid
        background = UIApplication.shared.beginBackgroundTask(withName: "upload") {
            UIApplication.shared.endBackgroundTask(background)
        }
        URLSession.shared.uploadTask(with: request, fromFile: file) { _, response, error in
            DispatchQueue.main.async {
                self.busy = false
                defer { UIApplication.shared.endBackgroundTask(background) }
                let status = (response as? HTTPURLResponse)?.statusCode
                guard status == 200 else {
                    // Kept, and retried on the next drain.
                    self.lastResult = "failed: " + (status.map { "HTTP \($0)" } ?? (error?.localizedDescription ?? "no response"))
                    log.error("upload of \(file.lastPathComponent) \(self.lastResult)")
                    return
                }
                try? FileManager.default.removeItem(at: file)
                self.lastResult = "sent at \(Date().formatted(date: .omitted, time: .standard))"
                self.drain()
            }
        }.resume()
    }
}
