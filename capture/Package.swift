// swift-tools-version:5.10
import Foundation
import PackageDescription

// The recorder runs under launchd, not from a terminal, so macOS attributes
// Bluetooth access to the binary itself: it needs its own Info.plist, with a
// usage description, linked into the executable.
let infoPlist = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("Info.plist").path

let package = Package(
    name: "hearsay-capture",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "hearsay-capture",
            path: "Sources",
            linkerSettings: [.unsafeFlags(["-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
                                           "-Xlinker", "__info_plist", "-Xlinker", infoPlist])]
        ),
    ]
)
