// PM OS Obsidian Link: opens a pm-os vault note in Obsidian without stealing focus.
//
// Why: in Claude Code inside
// cmux, a Cmd-clicked file:// link to a .md note opens cmux's own Markdown viewer
// (app.openMarkdownInCmuxViewer, on by default), and an obsidian:// link goes to
// NSWorkspace.open, which brings Obsidian to the front. Reports now link
// `pmos-obsidian://open?vault=<vault>&file=<note>`; cmux hands that scheme to
// LaunchServices, which starts this background-only app (LSBackgroundOnly, so it
// never takes focus). It rewrites the URL to obsidian://open with the same
// vault and file, opens it with `open -g -b md.obsidian` (Obsidian loads the
// note behind the app the owner is using), runs a 2-second focus guard like
// image_compare.py's, and quits.
//
// Hidden window comes forward: on one laptop screen the background open
// loaded the note behind cmux, so a click looked like it did nothing (the log
// showed each click and Obsidian's active tab was the note). Now the handler
// measures how much of Obsidian's main window is on screen and uncovered. At
// half or more it stays in the background with the focus guard. Below half
// (covered, minimized, another Space, not running) it opens Obsidian in front,
// because the click asked to see the note. Override with
// `defaults write com.pm-os.obsidian-link mode -string background|front|auto`.
// `pm-os-obsidian-link --decide` prints the measurement and choice, opens nothing.
// Test: system/scripts/test_obsidian_link_handler.py.
//
// Build and register: system/scripts/install-obsidian-link-handler.sh.
// Log: ~/Library/Logs/pm-os-obsidian-link.log.
import AppKit
import Foundation

let obsidianBundle = "md.obsidian"
let focusGuardSeconds = 2.0
let logURL = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Logs/pm-os-obsidian-link.log")

func log(_ message: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    let line = "\(stamp) \(message)\n"
    if let handle = try? FileHandle(forWritingTo: logURL) {
        handle.seekToEndOfFile()
        handle.write(line.data(using: .utf8)!)
        try? handle.close()
    } else {
        try? line.write(to: logURL, atomically: true, encoding: .utf8)
    }
}

/// obsidian://open?vault=…&file=… for a pmos-obsidian://open URL, else nil.
/// Only `vault` and `file` pass through, so the link can open a note and
/// nothing else (no obsidian://new, no plugin actions).
func obsidianURL(for url: URL) -> URL? {
    guard url.scheme?.lowercased() == "pmos-obsidian",
          url.host?.lowercased() == "open",
          let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
    let items = parts.queryItems ?? []
    guard let vault = items.first(where: { $0.name == "vault" })?.value, !vault.isEmpty,
          let file = items.first(where: { $0.name == "file" })?.value, !file.isEmpty else { return nil }
    var out = URLComponents()
    out.scheme = "obsidian"
    out.host = "open"
    out.queryItems = [URLQueryItem(name: "vault", value: vault), URLQueryItem(name: "file", value: file)]
    // URLComponents leaves `&` and `=` inside values alone; Obsidian needs them encoded.
    var allowed = CharacterSet.urlQueryAllowed
    allowed.remove(charactersIn: "&=+?#")
    out.percentEncodedQuery = out.queryItems!.map {
        "\($0.name)=\($0.value!.addingPercentEncoding(withAllowedCharacters: allowed)!)"
    }.joined(separator: "&")
    return out.url
}

func frontmostBundle() -> String? {
    NSWorkspace.shared.frontmostApplication?.bundleIdentifier
}

/// Share (0...1) of Obsidian's largest on-screen window that no normal window
/// in front of it covers. 0 when Obsidian is not running, minimized, hidden,
/// or on another Space. Samples a 16x16 grid of points; CoreGraphics lists
/// on-screen windows front to back, and window bounds need no Screen
/// Recording permission.
func obsidianVisibleFraction() -> Double {
    guard let pid = NSRunningApplication.runningApplications(withBundleIdentifier: obsidianBundle)
        .first?.processIdentifier,
        let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] else { return 0 }
    let me = ProcessInfo.processInfo.processIdentifier
    var above: [CGRect] = []
    for window in list {
        guard (window[kCGWindowLayer as String] as? Int) == 0,
              (window[kCGWindowAlpha as String] as? Double ?? 1) > 0.1,
              let bounds = window[kCGWindowBounds as String] as? NSDictionary,
              let rect = CGRect(dictionaryRepresentation: bounds),
              rect.width >= 40, rect.height >= 40 else { continue }
        let owner = window[kCGWindowOwnerPID as String] as? pid_t
        if owner == me { continue }
        if owner == pid {
            // The first big Obsidian window is the front one; smaller ones
            // (popovers, the title strip) are skipped.
            guard rect.width >= 300, rect.height >= 200 else { continue }
            let steps = 16
            var clear = 0
            for i in 0..<steps {
                for j in 0..<steps {
                    let point = CGPoint(x: rect.minX + rect.width * (Double(i) + 0.5) / Double(steps),
                                        y: rect.minY + rect.height * (Double(j) + 0.5) / Double(steps))
                    if !above.contains(where: { $0.contains(point) }) { clear += 1 }
                }
            }
            return Double(clear) / Double(steps * steps)
        }
        above.append(rect)
    }
    return 0
}

/// "background", "front", or "auto" (default) from the app's defaults domain.
func configuredMode() -> String {
    let mode = UserDefaults.standard.string(forKey: "mode")?.lowercased() ?? "auto"
    return ["background", "front", "auto"].contains(mode) ? mode : "auto"
}

/// True when the note should open with Obsidian in front.
func shouldOpenInFront(visible: Double, mode: String) -> Bool {
    switch mode {
    case "front": return true
    case "background": return false
    default: return visible < 0.5
    }
}

final class Handler: NSObject, NSApplicationDelegate {
    var handled = false

    func applicationWillFinishLaunching(_ notification: Notification) {
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleGetURL(_:withReply:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Launched with no URL (a double-click, lsregister): nothing to do.
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            if self?.handled != true { NSApp.terminate(nil) }
        }
    }

    @objc func handleGetURL(_ event: NSAppleEventDescriptor, withReply reply: NSAppleEventDescriptor) {
        handled = true
        guard let raw = event.paramDescriptor(forKeyword: keyDirectObject)?.stringValue,
              let url = URL(string: raw) else {
            log("refused: unreadable URL event")
            NSApp.terminate(nil)
            return
        }
        guard let target = obsidianURL(for: url) else {
            log("refused: \(raw) is not pmos-obsidian://open?vault=…&file=…")
            NSApp.terminate(nil)
            return
        }
        let before = frontmostBundle()
        let mode = configuredMode()
        let visible = obsidianVisibleFraction()
        let front = shouldOpenInFront(visible: visible, mode: mode)
        let open = Process()
        open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        open.arguments = (front ? [] : ["-g"]) + ["-b", obsidianBundle, target.absoluteString]
        do {
            try open.run()
            open.waitUntilExit()
        } catch {
            log("failed: open \(open.arguments!.joined(separator: " ")): \(error)")
            NSApp.terminate(nil)
            return
        }
        let measured = String(format: "visible=%.2f mode=%@", visible, mode)
        if front {
            // macOS can refuse an activation a background-only app asks for, so
            // ask Obsidian itself to come forward as well.
            NSRunningApplication.runningApplications(withBundleIdentifier: obsidianBundle).first?
                .activate(options: [.activateAllWindows])
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                log("opened \(target.absoluteString) in front (exit \(open.terminationStatus)), \(measured), front=\(before ?? "?") after=\(frontmostBundle() ?? "?")")
                NSApp.terminate(nil)
            }
            return
        }
        log("opened \(target.absoluteString) in background (exit \(open.terminationStatus)), \(measured), front=\(before ?? "?")")
        guardFocus(before: before)
    }

    /// For focusGuardSeconds after the open, if Obsidian comes to the front on
    /// its own, hand the front back to the app that had it. Another app coming
    /// forward means the owner switched on purpose, so the guard stops.
    func guardFocus(before: String?) {
        guard let before, before.lowercased() != obsidianBundle,
              ProcessInfo.processInfo.environment["PM_OS_FOCUS_GUARD_DISABLED"] != "1" else {
            NSApp.terminate(nil)
            return
        }
        let deadline = Date().addingTimeInterval(focusGuardSeconds)
        func tick() {
            let front = frontmostBundle()
            if front?.lowercased() == obsidianBundle {
                NSRunningApplication.runningApplications(withBundleIdentifier: before).first?.activate()
                log("focus_restored \(before)")
                NSApp.terminate(nil)
                return
            }
            if let front, front.lowercased() != before.lowercased() {
                NSApp.terminate(nil)
                return
            }
            if Date() >= deadline {
                NSApp.terminate(nil)
                return
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05, execute: tick)
        }
        tick()
    }
}

// Test hook: `--decide-for <visible> <mode>` prints the choice for given inputs.
if let i = CommandLine.arguments.firstIndex(of: "--decide-for"), CommandLine.arguments.count > i + 2,
   let visible = Double(CommandLine.arguments[i + 1]) {
    print(shouldOpenInFront(visible: visible, mode: CommandLine.arguments[i + 2]) ? "front" : "background")
    exit(0)
}

if CommandLine.arguments.contains("--decide") {
    let mode = configuredMode()
    let visible = obsidianVisibleFraction()
    let choice = shouldOpenInFront(visible: visible, mode: mode) ? "front" : "background"
    print(String(format: "visible=%.2f mode=%@ open=%@ front_app=%@", visible, mode, choice, frontmostBundle() ?? "?"))
    exit(0)
}

let app = NSApplication.shared
let handler = Handler()
app.delegate = handler
app.setActivationPolicy(.prohibited)
app.run()
