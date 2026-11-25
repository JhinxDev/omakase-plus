import SwiftUI

@main
struct OmakasePlusPhoneApp: App {
    private let syntheticPrototype = ProcessInfo.processInfo.arguments.contains("-OmakaseSyntheticPrototype")

    var body: some Scene {
        WindowGroup {
            Group {
                if syntheticPrototype {
                    PrototypeLibrary()
                        .tint(.purple)
                } else {
                    PhoneRootView()
                        .tint(.appAccent)
                }
            }
            .preferredColorScheme(.dark)
        }
    }
}
