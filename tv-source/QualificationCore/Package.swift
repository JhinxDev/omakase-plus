// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "QualificationCore",
    platforms: [.macOS(.v13), .tvOS(.v17), .iOS(.v17)],
    products: [.library(name: "QualificationCore", targets: ["QualificationCore"])],
    targets: [
        .target(name: "QualificationCore"),
        .testTarget(name: "QualificationCoreTests", dependencies: ["QualificationCore"])
    ]
)
