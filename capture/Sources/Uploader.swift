import Foundation

/// Sends sealed uploads to the capture receiver on the NAS, oldest first, one
/// at a time. The file name is the idempotency key, so an upload that reached
/// the NAS but whose answer was lost is recognized as a duplicate when resent.
final class Uploader {
    private let spool: Spool
    private let url: URL
    private let token: String
    private var busy = false

    init(spool: Spool, url: URL, token: String) {
        self.spool = spool
        self.url = url
        self.token = token
    }

    func drain() {
        guard !busy, let file = spool.pending().first else { return }
        busy = true
        var request = URLRequest(url: url, timeoutInterval: 60)
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(file.deletingPathExtension().lastPathComponent, forHTTPHeaderField: "Idempotency-Key")
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        URLSession.shared.uploadTask(with: request, fromFile: file) { _, response, error in
            DispatchQueue.main.async {
                self.busy = false
                let status = (response as? HTTPURLResponse)?.statusCode
                guard status == 200 else {
                    // Kept, and retried on the next drain.
                    log("upload of \(file.lastPathComponent) failed: \(status.map { "HTTP \($0)" } ?? (error.map { "\($0)" } ?? "no response"))")
                    return
                }
                try? FileManager.default.removeItem(at: file)
                self.drain()
            }
        }.resume()
    }
}
