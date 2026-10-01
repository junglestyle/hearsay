// swift-tools-version:5.10
import PackageDescription

// Built into an app bundle by install/mac.sh, which supplies Info.plist:
// macOS asks for mic and system audio access on behalf of the bundle.
let package = Package(
    name: "hearsay-mac",
    platforms: [.macOS("14.4")]  // process taps (14.2) and per-process input state (14.4),
    targets: [
        .executableTarget(name: "hearsay-mac", path: "Sources"),
    ]
)
