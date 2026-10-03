// Steward for macOS: a small native window around the Steward app that runs locally.
// Built on the user's Mac by scripts/install.sh with:  swiftc -O Steward.swift -o Steward
// (needs only the Xcode Command Line Tools, which Homebrew installs).
//
// The agent itself runs as a background service; this app just shows it, so closing the
// window never stops your scheduled tasks or Telegram.

import Cocoa
import UserNotifications
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                         WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var retries = 0
    var openAfterDownload = Set<ObjectIdentifier>()
    var downloadDestinations = [ObjectIdentifier: URL]()

    // Where the project lives (written into Info.plist by the installer).
    lazy var projectDir: String = {
        Bundle.main.object(forInfoDictionaryKey: "StewardProjectDir") as? String ?? ""
    }()
    let stateDir = NSHomeDirectory() + "/.steward"

    // MARK: - Launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.userContentController.add(self, name: "steward")
        config.userContentController.addUserScript(WKUserScript(
            source: "window.__STEWARD_NATIVE__ = true; document.documentElement.classList.add('native');",
            injectionTime: .atDocumentStart, forMainFrameOnly: true))

        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")   // no white flash while loading
        webView.allowsMagnification = true

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1220, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = appName()
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(red: 0.071, green: 0.071, blue: 0.071, alpha: 1)
        window.minSize = NSSize(width: 420, height: 560)
        window.contentView = webView
        window.delegate = self
        window.isReleasedWhenClosed = false
        if !window.setFrameUsingName("StewardMain") { window.center() }
        window.setFrameAutosaveName("StewardMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        loadApp()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window.makeKeyAndOrderFront(nil) }
        return true
    }

    // Closing the window quits this viewer only; the agent keeps running in the background.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func appName() -> String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Steward"
    }

    // MARK: - Connecting to the agent

    func readPort() -> Int {
        guard let env = try? String(contentsOfFile: projectDir + "/.env", encoding: .utf8) else { return 8765 }
        for line in env.split(separator: "\n") where line.hasPrefix("WEB_PORT=") {
            if let p = Int(line.dropFirst("WEB_PORT=".count).trimmingCharacters(in: .whitespaces)) { return p }
        }
        return 8765
    }

    func loadApp() {
        let token = (try? String(contentsOfFile: stateDir + "/web_token", encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !token.isEmpty, let url = URL(string: "http://127.0.0.1:\(readPort())/?t=\(token)") else {
            showWaiting("Starting \(appName())…")
            scheduleRetry()
            return
        }
        webView.load(URLRequest(url: url))
    }

    func scheduleRetry() {
        retries += 1
        if retries == 1 { kickstartAgent() }
        if retries > 40 {
            showWaiting("\(appName()) isn't starting. Double-click “Start Steward.command” in the Steward folder, then reopen this app.", spinner: false)
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in self?.loadApp() }
    }

    /// Ask launchd to (re)start the background agent if it isn't answering.
    func kickstartAgent() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", "gui/\(getuid())/com.steward.agent"]
        try? p.run()
    }

    func showWaiting(_ message: String, spinner: Bool = true) {
        let html = """
        <html><head><meta name="color-scheme" content="light dark"><style>
        body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;flex-direction:column;
             gap:18px;font:15px -apple-system,sans-serif;background:#121212;color:#bbb;text-align:center;padding:0 40px}
        @media (prefers-color-scheme: light){body{background:#f7f7f5;color:#555}}
        .s{width:22px;height:22px;border:2px solid #555;border-top-color:transparent;border-radius:50%;animation:r .8s linear infinite}
        @keyframes r{to{transform:rotate(360deg)}}</style></head>
        <body>\(spinner ? "<div class='s'></div>" : "")<div>\(message)</div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showWaiting("Starting \(appName())…")
        scheduleRetry()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView.url?.host == "127.0.0.1" { retries = 0 }
    }

    // MARK: - Links, downloads, file pickers, microphone

    func isLocal(_ url: URL?) -> Bool {
        guard let host = url?.host else { return true }       // about:blank, data:
        return host == "127.0.0.1" || host == "localhost"
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.shouldPerformDownload { decisionHandler(.download); return }
        if let url = navigationAction.request.url, !isLocal(url), navigationAction.navigationType == .linkActivated {
            NSWorkspace.shared.open(url)                       // web links open in your browser
            decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        decisionHandler(navigationResponse.canShowMIMEType ? .allow : .download)
    }

    // target="_blank": outside links go to the browser; the agent's own files are downloaded and opened.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard let url = navigationAction.request.url else { return nil }
        if isLocal(url) {
            webView.startDownload(using: URLRequest(url: url)) { [weak self] download in
                download.delegate = self
                self?.openAfterDownload.insert(ObjectIdentifier(download))
            }
        } else {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let folder = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        var dest = folder.appendingPathComponent(suggestedFilename)
        let base = dest.deletingPathExtension().lastPathComponent, ext = dest.pathExtension
        var n = 2
        while FileManager.default.fileExists(atPath: dest.path) {
            dest = folder.appendingPathComponent(ext.isEmpty ? "\(base) \(n)" : "\(base) \(n).\(ext)")
            n += 1
        }
        downloadDestinations[ObjectIdentifier(download)] = dest
        completionHandler(dest)
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let dest = downloadDestinations.removeValue(forKey: ObjectIdentifier(download)) else { return }
        if openAfterDownload.remove(ObjectIdentifier(download)) != nil {
            NSWorkspace.shared.open(dest)
        } else {
            NSWorkspace.shared.activateFileViewerSelecting([dest])
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        openAfterDownload.remove(ObjectIdentifier(download))
        downloadDestinations.removeValue(forKey: ObjectIdentifier(download))
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.beginSheetModal(for: window) { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(origin.host == "127.0.0.1" ? .grant : .deny)   // voice messages
    }

    // MARK: - Messages from the page (notifications, theme)

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        switch type {
        case "notify":
            guard !NSApp.isActive else { return }
            let content = UNMutableNotificationContent()
            content.title = body["title"] as? String ?? appName()
            content.body = body["body"] as? String ?? ""
            content.sound = .default
            UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
            NSApp.requestUserAttention(.informationalRequest)
        case "theme":
            if let hex = body["bg"] as? String, let color = NSColor(hex: hex) { window.backgroundColor = color }
            if let dark = body["dark"] as? Bool {
                window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
            }
        default:
            break
        }
    }

    // MARK: - Menus

    @objc func reload(_ sender: Any?) { retries = 0; loadApp() }
    @objc func zoomIn(_ sender: Any?) { webView.pageZoom = min(webView.pageZoom + 0.1, 2.0) }
    @objc func zoomOut(_ sender: Any?) { webView.pageZoom = max(webView.pageZoom - 0.1, 0.6) }
    @objc func zoomReset(_ sender: Any?) { webView.pageZoom = 1.0 }
    @objc func openFolder(_ sender: Any?) { NSWorkspace.shared.open(URL(fileURLWithPath: projectDir)) }

    func buildMenu() {
        let name = appName()
        let main = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About \(name)", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Open Steward Folder", action: #selector(openFolder(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide \(name)", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let others = appMenu.addItem(withTitle: "Hide Others", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        others.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "Show All", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit \(name)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        main.addItem(submenu(appMenu, title: name))

        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        main.addItem(submenu(edit, title: "Edit"))

        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Reload", action: #selector(reload(_:)), keyEquivalent: "r")
        view.addItem(.separator())
        view.addItem(withTitle: "Actual Size", action: #selector(zoomReset(_:)), keyEquivalent: "0")
        view.addItem(withTitle: "Zoom In", action: #selector(zoomIn(_:)), keyEquivalent: "+")
        view.addItem(withTitle: "Zoom Out", action: #selector(zoomOut(_:)), keyEquivalent: "-")
        view.addItem(.separator())
        let full = view.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        full.keyEquivalentModifierMask = [.command, .control]
        main.addItem(submenu(view, title: "View"))

        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        main.addItem(submenu(win, title: "Window"))
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }

    func submenu(_ menu: NSMenu, title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.submenu = menu
        return item
    }
}

extension NSColor {
    convenience init?(hex: String) {
        var s = hex.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        self.init(red: CGFloat((v >> 16) & 0xff) / 255, green: CGFloat((v >> 8) & 0xff) / 255,
                  blue: CGFloat(v & 0xff) / 255, alpha: 1)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
_ = app.setActivationPolicy(.regular)
app.run()
