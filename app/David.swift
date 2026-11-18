// David.app: a real Mac app around David. It starts the voice assistant (via
// start-david.command) as its own child, so macOS asks for the microphone,
// Screen Recording and Accessibility on behalf of "David" instead of Terminal,
// and it shows his screen in its own window instead of a browser.
// Built by app/build.sh, which writes the repository folder into Info.plist.

import Cocoa
import WebKit

let page = URL(string: "http://127.0.0.1:8765/david_ui.html")!

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var david: Process?
    var logURL: URL!
    var loaded = false
    var quitting = false

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")   // dark, not white, while he starts

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 800),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "David"
        window.backgroundColor = NSColor(srgbRed: 0.055, green: 0.055, blue: 0.086, alpha: 1)
        window.isReleasedWhenClosed = false
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("DavidMain")          // reopens where you left it
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        startDavid()
        web.load(URLRequest(url: page))
    }

    func startDavid() {
        guard let folder = Bundle.main.object(forInfoDictionaryKey: "DavidFolder") as? String else { return }
        let dir = URL(fileURLWithPath: folder)
        logURL = dir.appendingPathComponent("david.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)   // fresh log each start
        let out = try? FileHandle(forWritingTo: logURL)

        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [dir.appendingPathComponent("start-david.command").path]
        p.currentDirectoryURL = dir
        var env = ProcessInfo.processInfo.environment
        env["DAVID_APP"] = "1"            // David skips opening a browser window
        env["PYTHONUNBUFFERED"] = "1"
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        p.environment = env
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = out
        p.standardError = out
        p.terminationHandler = { proc in
            DispatchQueue.main.async { self.davidStopped(proc.terminationStatus) }
        }
        do { try p.run(); david = p } catch { davidStopped(-1) }
    }

    // start-david.command exits 0 straight away when David is already running
    // (from Terminal, say): then this window just shows that copy.
    func davidStopped(_ status: Int32) {
        david = nil
        if status == 0 || quitting { return }
        let tail = (try? String(contentsOf: logURL, encoding: .utf8))?
            .split(separator: "\n").suffix(12).joined(separator: "\n") ?? ""
        let alert = NSAlert()
        alert.messageText = "David stopped"
        alert.informativeText = tail.isEmpty ? "No log was written." : tail
        alert.addButton(withTitle: "Quit")
        alert.runModal()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ note: Notification) {
        quitting = true
        // start-david.command ends by exec-ing Python, so this stops him. No waitUntilExit:
        // it never returns while the app is quitting, and the signal alone is enough.
        david?.terminate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)   // closing the window keeps him listening; the Dock icon brings it back
        return true
    }

    // His server takes a few seconds to come up: keep trying until the page loads.
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        if loaded { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { w.load(URLRequest(url: page)) }
    }

    func webView(_ w: WKWebView, didFinish n: WKNavigation!) { loaded = true }

    // Only web links ever leave the app — never file:// or other apps' link schemes.
    func isWeb(_ url: URL) -> Bool { ["http", "https"].contains(url.scheme?.lowercased() ?? "") }
    func isDavid(_ url: URL) -> Bool {
        url.scheme == "http" && ["127.0.0.1", "localhost"].contains(url.host ?? "") && url.port == 8765
    }

    // David's own pages stay in the window; other web links open in the normal browser;
    // anything else is refused.
    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if isDavid(url) || url.absoluteString == "about:blank" { return decisionHandler(.allow) }
        if isWeb(url) { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)
    }

    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures f: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, isWeb(url) { NSWorkspace.shared.open(url) }   // target="_blank" links
        return nil
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(), app = NSMenu()
        app.addItem(withTitle: "Hide David", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(.separator())
        app.addItem(withTitle: "Quit David", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = app
        main.addItem(appItem)

        // Without an Edit menu, copy and paste don't work in the chat box.
        let editItem = NSMenuItem(), edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let winItem = NSMenuItem(), win = NSMenu(title: "Window")
        win.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.addItem(withTitle: "Minimise", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Reload", action: #selector(WKWebView.reload(_:)), keyEquivalent: "r")
        winItem.submenu = win
        main.addItem(winItem)
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
