// hearsay-capture: records the Omi pendant over BLE and uploads what it sends
// to Hearsay's capture receiver on the NAS, over the tailnet. See Spool for
// the upload format and hearsay/capture.py for how the NAS reads it.
//
// Config comes from an env file outside the repo (install/mac.sh writes it):
//   HEARSAY_CAPTURE_URL    http://<nas tailnet ip>:8789/capture
//   HEARSAY_CAPTURE_TOKEN  bearer token from the NAS's capture.env
//   HEARSAY_CAPTURE_SPOOL  optional; default ~/.local/share/hearsay/capture-spool

import Foundation

func log(_ message: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    print("\(stamp) \(message)")
    fflush(stdout)
}

func readEnv(_ path: String) -> [String: String] {
    guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return [:] }
    var env: [String: String] = [:]
    for line in text.split(separator: "\n") {
        guard !line.hasPrefix("#"), let eq = line.firstIndex(of: "=") else { continue }
        env[String(line[..<eq])] = String(line[line.index(after: eq)...])
    }
    return env
}

let home = FileManager.default.homeDirectoryForCurrentUser.path
let envPath = CommandLine.arguments.dropFirst().first ?? "\(home)/.config/hearsay/capture.env"
let env = readEnv(envPath)
guard let urlText = env["HEARSAY_CAPTURE_URL"], let url = URL(string: urlText),
      let token = env["HEARSAY_CAPTURE_TOKEN"], !token.isEmpty else {
    log("missing HEARSAY_CAPTURE_URL or HEARSAY_CAPTURE_TOKEN in \(envPath); run install/mac.sh")
    exit(1)
}
let spoolDir = URL(fileURLWithPath: env["HEARSAY_CAPTURE_SPOOL"] ?? "\(home)/.local/share/hearsay/capture-spool")

let spool: Spool
do {
    spool = try Spool(dir: spoolDir)
} catch {
    log("can't use the spool at \(spoolDir.path): \(error)")
    exit(1)
}
let uploader = Uploader(spool: spool, url: url, token: token)
let pendant = Pendant(spool: spool)
log("recording to \(spoolDir.path), uploading to \(url)")

// Uploads cover a minute each; the NAS reprocesses hourly, so there's no
// point in smaller ones.
Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in spool.sync() }
Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in
    spool.seal()
    pendant.report()
}
Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { _ in uploader.drain() }
uploader.drain()
RunLoop.main.run()
