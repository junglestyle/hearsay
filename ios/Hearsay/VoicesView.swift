import SwiftUI
import WebKit

/// Naming speakers: the NAS's portal (hearsay/portal.py), which already
/// plays each cluster's samples and applies names. Shown here rather than
/// rebuilt, so there is one place where naming works. The portal is on the
/// tailnet only, so the samples never leave Hearsay's boundary.
///
/// The app logs in with the capture token from Settings, whenever the portal
/// would ask for a login, so its login page only shows if the token is
/// refused.
struct VoicesView: View {
    @State private var reload = 0

    var body: some View {
        NavigationStack {
            Group {
                if let url = Settings.portalURL {
                    PortalView(home: url, reload: reload)
                        .ignoresSafeArea(edges: .bottom)
                } else {
                    ContentUnavailableView("No portal address", systemImage: "person.wave.2",
                                           description: Text("Enter it under Recorder > Server. install/ios.sh prints it."))
                }
            }
            .navigationTitle("Voices")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                Button { reload += 1 } label: { Image(systemName: "house") }
            }
        }
    }
}

private struct PortalView: UIViewRepresentable {
    let home: URL
    let reload: Int

    func makeCoordinator() -> Coordinator { Coordinator(home: home) }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.allowsInlineMediaPlayback = true
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        view.allowsBackForwardNavigationGestures = true
        context.coordinator.open(view)
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        // The home button, or a new portal address.
        guard reload != context.coordinator.reload || home != context.coordinator.home else { return }
        context.coordinator.reload = reload
        context.coordinator.home = home
        context.coordinator.open(view)
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var home: URL
        var reload = 0

        // Set when the portal refused the token, so its own login page shows.
        private var tokenRefused = false

        init(home: URL) { self.home = home }

        func open(_ view: WKWebView) {
            tokenRefused = false
            logIn(view)
        }

        /// The portal answers with a session cookie and a redirect home.
        private func logIn(_ view: WKWebView) {
            guard let token = Settings.token else {
                view.load(URLRequest(url: home))
                return
            }
            var request = URLRequest(url: home.appendingPathComponent("login/app"))
            request.httpMethod = "POST"
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            view.load(request)
        }

        /// Stay on the portal, and log in with the token instead of showing
        /// the login page.
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction) async -> WKNavigationActionPolicy {
            guard let url = action.request.url else { return .cancel }
            // about:blank is the error page below.
            if url.scheme == "about" { return .allow }
            guard url.host == home.host && url.port == home.port else { return .cancel }
            if url.path == "/login" && action.request.httpMethod == "GET" && !tokenRefused && Settings.token != nil {
                logIn(webView)
                return .cancel
            }
            return .allow
        }

        func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse) async -> WKNavigationResponsePolicy {
            // 401 for a wrong token; 404 or 405 from a portal without token login.
            guard response.response.url?.path == "/login/app",
                  ((response.response as? HTTPURLResponse)?.statusCode ?? 0) >= 400 else { return .allow }
            log.error("the portal refused the capture token; showing its login page")
            tokenRefused = true
            webView.load(URLRequest(url: home.appendingPathComponent("login")))
            return .cancel
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            let message = "Can't reach the portal at \(home.absoluteString): \(error.localizedDescription). Is Tailscale on?"
            webView.loadHTMLString("<meta name=viewport content='width=device-width'><p style='font:17px system-ui;padding:1rem'>"
                                   + message.replacingOccurrences(of: "<", with: "&lt;") + "</p>", baseURL: nil)
        }
    }
}
