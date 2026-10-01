// swift-tools-version:5.10
import PackageDescription

// Built into an app bundle by install/mac.sh, which supplies Info.plist:
// macOS asks for mic and system audio access on behalf of the bundle.
let package = Package(
    name: "hearsay-mac",
    // Process taps need 14.2, per-process input state (Zoom detection) 14.4.
    platforms: [.macOS("14.4")],
    targets: [
        .executableTarget(name: "hearsay-mac", path: "Sources"),
    ]
)
