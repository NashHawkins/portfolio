import SwiftUI
import UIKit

@main
struct LipSyncApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        _ = R2Uploader.shared // reconnect to an upload that kept running while the app was closed
        return true
    }

    /// iOS relaunched the app because a background upload finished.
    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        R2Uploader.shared.backgroundCompletionHandler = completionHandler
    }
}
