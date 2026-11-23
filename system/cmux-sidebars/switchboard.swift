// switchboard sidebar v2 — sectioned status board for switchboard workspaces.
//
// Sections (auto-assigned every ~1s):
//   📌 Pinned          — w.pinned (CoS, Todo, Comms Triage, …)
//   🚨 Needs attention — agent blocked on a prompt (◆) or red signal color
//   ⚡ Active          — agent running (braille spinner) or yellow signal
//   👁 To review       — purple signal ONLY (waiting on the owner to review/
//                        approve). The review-lane amp plugin paints
//                        finished workspaces purple and holds that color
//                        until the owner types a response, so merely clicking the
//                        row (which clears unread) does NOT drop it out of
//                        this lane. Raw unread is deliberately NOT a trigger:
//                        background startup seeding always leaves unread
//                        output, which would put hydrated P0 rows in To review.
//                        The plugin is the sole review authority. It pins
//                        genuine finished turns and skips hydration turns.
//   🔥 P0 to-dos       — idle/waiting fallback for title-tagged "P0 ·" rows
//   🌊 Waiting         — blue signal color (waiting/scheduled/needs follow-up)
//   ⏸ Paused          — gray signal color
//   💤 Idle            — everything else (stable/reference), most recent
//                        activity first
//   ⚙️ Background       — utility workspaces (Scheduled Jobs, Meetings
//                        Registry, Router 1–5) pinned here by title,
//                        whatever their state
//
// Lane precedence: Pinned / Background are explicit ownership rules. Every
// other row is classified by live agent state first, then published signals;
// P0 is
// only the fallback for a P0-tagged row with no transient action state.
//
// Signal channel: publish-workspace-status.sh mirrors each workspace's switchboard
// signal onto the workspace `color` field, and writes the next-action summary
// into the workspace `description`. Those are the only two pipeline fields
// the custom-sidebar API exposes on cmux 0.64.x (status pills are not
// readable here). Colors must stay in sync with COLOR{} in that script:
//   #FFB3AC red=blocked/needs review   #FFF0A8 yellow=active
//   #BB9AF7 purple=to review           #C2EAFF blue=waiting
//   #D7DEE7 gray=paused                #BDF5C2 green=stable/reference
//   #6A2F3A legacy urgent fill
//
// Install: link this file into cmux's sidebars folder as switchboard.swift.
//
// After editing this file, run `cmux sidebar reload switchboard`: an atomic save
// does not always trigger a reload, and a failed interpretation silently
// keeps the last good render. Use `cmux sidebar validate switchboard` to check for
// DSL errors.
//
// NOTE: the sidebar interpreter does not handle the `nil` literal in every
// position — branch presence with `if let` where possible.

// ---- Agent title helpers ----
//
// Agent CLIs put a status glyph in the tab title: a braille spinner cell
// while working, ◆ while blocked on a permission prompt, or a star/dot when
// idle. Amp also appends " - amp - <cwd>". These helpers read that state and
// strip the decoration for display.

// First space-separated token of a title, or "" when there is none.
func leadToken(_ t: String) -> String {
    if t == "" { return "" }
    let parts = t.split(" ")
    if parts.count == 0 { return "" }
    return parts[0]
}

// True for any single cell of the braille block (U+2800 to U+28FF). The DSL
// has no scalar ranges, so the full block is spelled out as a lookup string.
func isBrailleCell(_ s: String) -> Bool {
    let cells = "⠀⠁⠂⠃⠄⠅⠆⠇⠈⠉⠊⠋⠌⠍⠎⠏⠐⠑⠒⠓⠔⠕⠖⠗⠘⠙⠚⠛⠜⠝⠞⠟⠠⠡⠢⠣⠤⠥⠦⠧⠨⠩⠪⠫⠬⠭⠮⠯⠰⠱⠲⠳⠴⠵⠶⠷⠸⠹⠺⠻⠼⠽⠾⠿⡀⡁⡂⡃⡄⡅⡆⡇⡈⡉⡊⡋⡌⡍⡎⡏⡐⡑⡒⡓⡔⡕⡖⡗⡘⡙⡚⡛⡜⡝⡞⡟⡠⡡⡢⡣⡤⡥⡦⡧⡨⡩⡪⡫⡬⡭⡮⡯⡰⡱⡲⡳⡴⡵⡶⡷⡸⡹⡺⡻⡼⡽⡾⡿⢀⢁⢂⢃⢄⢅⢆⢇⢈⢉⢊⢋⢌⢍⢎⢏⢐⢑⢒⢓⢔⢕⢖⢗⢘⢙⢚⢛⢜⢝⢞⢟⢠⢡⢢⢣⢤⢥⢦⢧⢨⢩⢪⢫⢬⢭⢮⢯⢰⢱⢲⢳⢴⢵⢶⢷⢸⢹⢺⢻⢼⢽⢾⢿⣀⣁⣂⣃⣄⣅⣆⣇⣈⣉⣊⣋⣌⣍⣎⣏⣐⣑⣒⣓⣔⣕⣖⣗⣘⣙⣚⣛⣜⣝⣞⣟⣠⣡⣢⣣⣤⣥⣦⣧⣨⣩⣪⣫⣬⣭⣮⣯⣰⣱⣲⣳⣴⣵⣶⣷⣸⣹⣺⣻⣼⣽⣾⣿"
    return s.count == 1 && cells.contains(s)
}

// True when the token is a single status glyph: braille, star, dot or ◆.
func isStatusGlyph(_ s: String) -> Bool {
    let marks = "✳✻✽✢✶✷✸✹✺·•∗*◆"
    if s.count != 1 { return false }
    return isBrailleCell(s) || marks.contains(s)
}

func agentBusy(_ title: String) -> Bool {
    return isBrailleCell(leadToken(title))
}

func agentAttention(_ title: String) -> Bool {
    return title.hasPrefix("◆")
}

// A title an agent CLI wrote: status glyph first, or the Amp cwd suffix.
func looksLikeAgentTitle(_ t: String) -> Bool {
    return isStatusGlyph(leadToken(t)) || t.contains(" - amp - ")
}

// Drop a leading status glyph; keep the input if nothing would remain.
func dropStatusGlyph(_ t: String) -> String {
    if !isStatusGlyph(leadToken(t)) { return t }
    let rest = t.split(" ").dropFirst().joined(" ")
    return rest.isEmpty ? t : rest
}

// Drop Amp's trailing " - amp - <cwd>" (the last four tokens); keep the
// input if nothing would remain.
func dropAmpSuffix(_ t: String) -> String {
    if !t.contains(" - amp - ") { return t }
    let kept = t.split(" ").dropLast(4).joined(" ")
    return kept.isEmpty ? t : kept
}

func cleanTitle(_ title: String) -> String {
    return dropAmpSuffix(dropStatusGlyph(title))
}

func tabsBusy(_ tabs: [Any]) -> Bool {
    return tabs.filter { agentBusy($0.title) }.count > 0
}

func tabsAttention(_ tabs: [Any]) -> Bool {
    return tabs.filter { agentAttention($0.title) }.count > 0
}

// Split a leading emoji off the title so it can render as a separate element:
// wrapped title lines then align with each other instead of starting under
// the emoji. Detection: first token is one grapheme (String.count is
// grapheme-based here) that isn't printable ASCII. Note: String.prefix(n) is
// NOT supported by the sidebar interpreter — don't reintroduce it.
func titleIcon(_ t: String) -> String {
    let ascii = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,:;!?#@$%&*+=<>/()[]{}|~^_-"
    if t == "" { return "" }
    let first = t.split(" ").first
    if first.count == 1 && !ascii.contains(first) { return first }
    return ""
}

func titleText(_ t: String) -> String {
    if titleIcon(t) == "" { return stripP0(t) }
    let rest = t.split(" ").dropFirst().joined(" ")
    return rest.isEmpty ? stripP0(t) : stripP0(rest)
}

// The "P0 ·" tag drives bucketing (isP0) but is noise in the row label.
func stripP0(_ t: String) -> String {
    if t.hasPrefix("P0 · ") {
        let rest = t.split(" ").dropFirst(2).joined(" ")
        return rest.isEmpty ? t : rest
    }
    return t
}

// switchboard names its workspaces, so a non-empty workspace title always wins.
// Only an unnamed workspace borrows the cleaned title of its first agent tab.
func pickTitle(_ tabs: [Any], _ fallback: String) -> String {
    if !fallback.isEmpty { return fallback }
    let named = tabs.filter { looksLikeAgentTitle($0.title) }.map { cleanTitle($0.title) }
    if named.count > 0 { return named[0] }
    return fallback
}

// ---- Bucketing ----

func isBusy(_ w) -> Bool {
    return tabsBusy(w.tabs) || agentBusy(w.title)
}

func isAttention(_ w) -> Bool {
    return tabsAttention(w.tabs) || agentAttention(w.title)
}

func signalHex(_ w) -> String {
    if let c = w.color { return c.lowercased() }
    return ""
}

// Background utility workspaces stay in their own section no matter what
// state they're in — even while actively running. Matched by title.
// P0 to-do workspaces are title-tagged with "P0 ·". The tag supplies their
// fallback lane only; live attention, active, and review states take priority.
func isP0(_ w) -> Bool {
    let t = pickTitle(w.tabs, cleanTitle(w.title))
    return t.contains("P0 ·")
}

func isBackground(_ w) -> Bool {
    let t = cleanTitle(w.title)
    let name = titleText(t)
    let isRouter = name == "Router 1" || name == "Router 2" || name == "Router 3" || name == "Router 4" || name == "Router 5"
    return t.contains("Scheduled Jobs") || t.contains("Meetings Registry") || isRouter
}

// Epoch of the workspace's last agent/prompt activity; 0 when it has none.
// `latestAt` is optional in the sidebar API, so branch with `if let`.
func lastActivity(_ w) -> Double {
    if let t = w.latestAt { return t }
    return 0
}

// Deterministic within-lane sort key, so rows never jump.
// Lanes must NOT inherit cmux's physical list order: cmux spontaneously
// reorders rows (active-workspace bumps) and the arrange-sidebar cascade in
// cmux-helpers.sh re-fires move-tops, so raw order mutates constantly and
// rows would swap inside a lane.
// Key = spinner-stripped title (cleanTitle, so live braille spinners can't
// flip two rows) lowercased, with the workspace id as tiebreaker.
func laneKey(_ w) -> String {
    return cleanTitle(w.title).lowercased() + "|" + w.id
}

// Known residual (deliberate non-fix): ad-hoc amp rows can blink
// active↔idle for a few seconds mid-turn because Amp's TUI drops the braille
// title spinner between agent steps. The DSL re-evaluates ~1s with no
// persistent state and no file access, so hysteresis is impossible here, and
// smuggling busy state through the color field would add a third color
// writer, the race class the publishers avoid. The
// real fix belongs upstream (cmux busy tracking or Amp's title).
//
// Precedence: live agent state beats the published pipeline signal.
// To review is driven by review purple ONLY. The review-lane plugin paints
// genuine finished turns purple and deliberately skips startup hydration
// turns. Raw unread must not bucket here: every background launch leaves
// unread output, which would put start-of-day P0 rows in To review.
func bucketOf(_ w) -> String {
    if isAttention(w) { return "attention" }
    if isBusy(w) { return "active" }
    let c = signalHex(w)
    if c == "#ffb3ac" || c == "#6a2f3a" { return "attention" }
    if c == "#bb9af7" { return "toreview" }
    if c == "#fff0a8" { return "active" }
    if c == "#c2eaff" { return "waiting" }
    if c == "#d7dee7" { return "paused" }
    return "idle"
}

// ---- Views ----

func sectionHeader(_ icon: String, _ name: String, _ count: Int, _ tint, _ rule: Bool) -> some View {
    VStack(spacing: 0) {
        if rule {
            Rectangle().foregroundColor("#000000").opacity(0.0).frame(height: 8)
            Divider()
        }
        Rectangle().foregroundColor("#000000").opacity(0.0).frame(height: 5)
        HStack(spacing: 6) {
            Image(systemName: icon).font(.system(size: 11)).foregroundColor(tint)
            Text(name).font(.system(size: 11)).fontWeight(.semibold).textCase(.uppercase).foregroundColor(tint).lineLimit(1).fixedSize()
            Text("\(count)").font(.system(size: 10, design: .monospaced)).foregroundColor(tint)
                .padding(3)
                .background { Capsule().foregroundColor(tint).opacity(0.18) }
        }
    }
}

// NOTE: the custom-sidebar DSL cannot observe middle clicks (onMiddleClick is
// not a registered modifier and is silently ignored; native middle-click-close
// only exists in the built-in sidebar). Each row gets a small ✕ button instead.
//
// Row select is an .onTapGesture on a plain HStack, NOT a Button: the
// interpreter renders rich-label Buttons with .buttonStyle(.plain) and no
// content shape, so only the opaque text/glyphs were clickable (contentShape
// and buttonStyle are unregistered modifiers — silently ignored). For
// non-Button nodes carrying an action, RenderNodeView adds
// .contentShape(Rectangle()) across the node's full bounds, so with
// .frame(maxWidth: .infinity) the ENTIRE row is clickable.
func row(_ w, _ tint) -> some View {
    HStack(alignment: .top, spacing: 2) {
        HStack(alignment: .top, spacing: 7) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .top, spacing: 6) {
                    if let r = w.remote {
                        Image(systemName: "cloud.fill").font(.system(size: 11)).foregroundColor("#4C8DFF")
                        Circle()
                            .fill(r.connected ? "#37C871" : (r.state == "connecting" || r.state == "reconnecting" ? "#F5A623" : (r.state == "error" || r.state == "suspended" ? "#FF5C5C" : "#9AA0A6")))
                            .frame(width: 6, height: 6)
                    }
                    if w.title.hasPrefix("⏳") {
                        Image(systemName: "cloud").font(.system(size: 11)).foregroundColor("#F5A623")
                    }
                    if titleIcon(pickTitle(w.tabs, cleanTitle(w.title))) != "" {
                        Text(titleIcon(pickTitle(w.tabs, cleanTitle(w.title))))
                            .font(.system(size: 14))
                    }
                    Text(titleText(pickTitle(w.tabs, cleanTitle(w.title))))
                        .font(.system(size: 14))
                        .fontWeight(w.selected ? .semibold : .regular)
                        .foregroundColor(w.selected ? .accent : .primary)
                        .lineLimit(2)
                        .lineSpacing(3)
                        .multilineTextAlignment(.leading)
                        .help(titleText(pickTitle(w.tabs, cleanTitle(w.title))))
                    Spacer()
                    if isAttention(w) {
                        Image(systemName: "exclamationmark.circle.fill").font(.system(size: 12)).foregroundColor("#F5A623")
                    } else if isBusy(w) {
                        Image(systemName: "bolt.fill").font(.system(size: 12)).foregroundColor("#37C871")
                    } else if w.unread > 0 {
                        Text("\(w.unread)")
                            .font(.system(size: 10, design: .monospaced)).bold()
                            .foregroundColor("#1A1A22")
                            .padding(3)
                            .background { Circle().foregroundColor("#E0AF68") }
                    }
                }
            }
        }
        .padding(5)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background { RoundedRectangle(cornerRadius: 6).foregroundColor(w.selected ? "#4C8DFF" : "#000000").opacity(w.selected ? 0.18 : 0.0) }
        .onTapGesture { cmux("workspace.select", workspace_id: w.id) }
    // Padding alone is NOT hit-testable in SwiftUI (verified: clicks in the
    // transparent padding did nothing), and the sidebar DSL has no
    // .contentShape. The near-invisible background fills the padded bounds
    // and makes the whole ~24pt area a real tap target; glyph stays 8pt.
    Image(systemName: "xmark").font(.system(size: 8)).foregroundColor("#4A4F57").padding(8)
        // opacity must be >= 0.01: SwiftUI treats views below that as
        // fully transparent and skips them for hit-testing
        .background { Rectangle().foregroundColor("#000000").opacity(0.02) }
        .onTapGesture {
            // The DSL has no confirmation dialog, and workspace.close refuses
            // a workspace with a running process unless forced. The helper tab
            // asks "Close …?" natively, then unpins and force-closes on Close.
            cmux("surface.create", workspace_id: w.id, initial_command: "/bin/bash {{SWITCHBOARD_ROOT}}/system/scripts/cmux-close-workspace.sh --exec \(w.id)")
        }
        .help("Close workspace")
    }
    .contextMenu {
        if w.pinned {
            Button("Unpin") { cmux("workspace.action", action: "unpin", workspace_id: w.id) }
        } else {
            Button("Pin") { cmux("workspace.action", action: "pin", workspace_id: w.id) }
        }
        Button("Move to top") { cmux("workspace.action", action: "move-top", workspace_id: w.id) }
        // The sidebar DSL has no text input, so an unfocused helper tab in
        // this workspace shows a native dialog, applies the rename, and
        // closes itself without replacing the active terminal.
        Button("Rename…") { cmux("surface.create", workspace_id: w.id, initial_command: "/bin/bash {{SWITCHBOARD_ROOT}}/system/scripts/cmux-rename-workspace.sh --exec \(w.id)") }
        // Bindings are not exposed to the 0.64.x sidebar DSL, so this stays
        // visible for every row. The helper copies only a validated Amp
        // resume binding and otherwise reports failure without touching the
        // clipboard; its unfocused tab self-closes after visible feedback.
        Button("Copy thread URL") { cmux("surface.create", workspace_id: w.id, initial_command: "/bin/bash {{SWITCHBOARD_ROOT}}/system/scripts/cmux-relaunch-workspace.sh --copy-thread-url \(w.id)") }
        // Mark read releases the sticky To-review hold: the review-lane
        // plugin paints rows purple (#BB9AF7) and bucketOf keeps purple rows
        // in To review even at unread 0. Clearing the color drops the row to
        // Idle. Only clear when the row is actually review-purple so a
        // meaningful signal color (waiting blue, paused gray) survives.
        if signalHex(w) == "#bb9af7" {
            Button("Mark read") {
                cmux("workspace.action", action: "mark-read", workspace_id: w.id)
                cmux("workspace.action", action: "clear-color", workspace_id: w.id)
            }
        } else {
            Button("Mark read") { cmux("workspace.action", action: "mark-read", workspace_id: w.id) }
        }
        Button("Mark unread") { cmux("workspace.action", action: "mark-unread", workspace_id: w.id) }
        // Relaunch: spawn a fresh workspace that adopts this one's title,
        // closes it (context capture still fires), and resumes its amp
        // thread — or a shell in its cwd — in a clean terminal.
        Button("Relaunch") { cmux("workspace.create", initial_command: "/bin/bash {{SWITCHBOARD_ROOT}}/system/scripts/cmux-relaunch-workspace.sh --exec \(w.id)") }
        // The disposable coordinator snapshots workspace IDs once, then
        // relaunches every entry except itself. Active/draft Amp sessions,
        // pinned/system rows, and plain terminals are all included.
        Button("Relaunch all") { cmux("workspace.create", initial_command: "/bin/bash {{SWITCHBOARD_ROOT}}/system/scripts/cmux-relaunch-workspace.sh --all \(w.id)") }
        Button("Close workspace…") { cmux("surface.create", workspace_id: w.id, initial_command: "/bin/bash {{SWITCHBOARD_ROOT}}/system/scripts/cmux-close-workspace.sh --exec \(w.id)") }
        // Close-others is destructive in bulk, so it's a two-step submenu:
        // the action only fires from the explicit confirm item. (The DSL has
        // no state or .alert/.confirmationDialog, so a nested Menu is the
        // only in-sidebar confirmation primitive.)
        Menu("Close others") {
            Button("Are you sure? Close all other workspaces") { cmux("workspace.action", action: "close-others", workspace_id: w.id) }
        }
    }
}

func lane(_ ws, _ icon: String, _ name: String, _ tint, _ rule: Bool) -> some View {
    VStack(alignment: .leading, spacing: 8) {
        if ws.count > 0 {
            sectionHeader(icon, name, ws.count, tint, rule)
            ForEach(ws.prefix(30)) { w in
                row(w, tint)
            }
            if ws.count > 30 {
                Text("+\(ws.count - 30) more")
                    .font(.system(size: 10)).foregroundColor("#6E7681")
                    .padding(.horizontal, 10)
            }
        }
    }
}

// clock.weekday is a Cocoa weekday Int (1=Sun … 7=Sat).
func dayName(_ d: Int) -> String {
    let names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    if d >= 1 && d <= 7 { return names[d - 1] }
    return ""
}

// Last 2 path components ("code/switchboard") so long paths fit the sidebar.
func shortPath(_ p: String) -> String {
    let parts = p.split("/")
    if parts.count > 2 {
        return parts.dropFirst(parts.count - 2).joined("/")
    }
    return p
}

VStack(alignment: .leading, spacing: 2) {
    // Header block gets its own tight VStack: rows placed directly in the
    // root VStack rendered with far more vertical space than spacing:2.
    VStack(alignment: .leading, spacing: 3) {
        HStack(spacing: 8) {
            Text("switchboard").font(.system(size: 14)).bold()
            Spacer()
            Button(action: { cmux("workspace.create") }) {
                Image(systemName: "plus").font(.system(size: 11)).padding(4).background("#2A2A2E").cornerRadius(5)
            }.help("New local workspace")
            Button(action: { cmux("workspace.create", initial_command: "/bin/bash {{SWITCHBOARD_ROOT}}/system/scripts/cmux-reopen-last-closed.sh --exec") }) {
                Image(systemName: "arrow.uturn.backward").font(.system(size: 11)).padding(4).background("#2A2A2E").cornerRadius(5)
            }.help("Reopen last closed workspace")
        }

        // Status strip: live clock + workspace stats, then the selected
        // workspace's folder and git branch. Re-renders ~1s so the clock is live.
        HStack(spacing: 4) {
            Image(systemName: "clock").font(.system(size: 9)).foregroundColor("#6E7681")
            Text("\(dayName(clock.weekday)) \(clock.time.split(":").prefix(2).joined(":"))").font(.system(size: 10, design: .monospaced)).foregroundColor("#9AA0A6")
            Text("· \(workspaceCount) ws").font(.system(size: 10)).foregroundColor("#6E7681")
            if unreadTotal > 0 {
                Text("· \(unreadTotal) unread").font(.system(size: 10)).foregroundColor("#E0AF68")
            }
            Spacer()
        }
        ForEach(workspaces.filter { $0.selected }.prefix(1)) { s in
            HStack(spacing: 4) {
                Image(systemName: "folder").font(.system(size: 9)).foregroundColor("#6E7681")
                Text(shortPath(s.directory)).font(.system(size: 10)).foregroundColor("#9AA0A6").lineLimit(1)
                if let b = s.branch {
                    Text("⎇ \(b)\(s.dirty ? "*" : "")").font(.system(size: 10)).foregroundColor("#7AA2F7").lineLimit(1)
                }
                Spacer()
            }
        }
    }
    // No horizontal padding: header shares the exact right margin of the row
    // ✕ column (root .padding(6) only), so the action chips line up with the
    // rest of the right rail instead of sitting 4pt inside it.
    .frame(maxWidth: .infinity, alignment: .leading)

    let all = workspaces.prefix(80)
    // Explicit pin always wins: a pinned workspace shows in the Pinned lane
    // even when its title matches the P0 or Background rules. Otherwise the
    // context-menu Pin action appears to do nothing for those rows.
    let pinnedWs = all.filter { $0.pinned }
    let background = all.filter { isBackground($0) && !$0.pinned }.sorted { laneKey($0) < laneKey($1) }
    let candidates = all.filter { !$0.pinned && !isBackground($0) }
    // Every lane is sorted on laneKey (stable, spinner-proof) so rows never
    // shuffle when cmux's physical list order mutates. Pinned keeps physical
    // order (curated: CoS, Todo, Comms). Idle keeps recency order.
    let attention = candidates.filter { bucketOf($0) == "attention" }.sorted { laneKey($0) < laneKey($1) }
    let active = candidates.filter { bucketOf($0) == "active" }.sorted { laneKey($0) < laneKey($1) }
    let toReview = candidates.filter { bucketOf($0) == "toreview" }.sorted { laneKey($0) < laneKey($1) }
    let p0 = candidates.filter {
        isP0($0) && bucketOf($0) != "attention" && bucketOf($0) != "active" && bucketOf($0) != "toreview"
    }.sorted { laneKey($0) < laneKey($1) }
    let rest = candidates.filter { !isP0($0) }
    let waiting = rest.filter { bucketOf($0) == "waiting" }.sorted { laneKey($0) < laneKey($1) }
    let paused = rest.filter { bucketOf($0) == "paused" }.sorted { laneKey($0) < laneKey($1) }
    let idle = rest.filter { bucketOf($0) == "idle" }.sorted { lastActivity($0) > lastActivity($1) }

    lane(pinnedWs, "pin.fill", "Pinned", "#9AA0A6", false)
    lane(attention, "exclamationmark.circle.fill", "Needs attention", "#F5A623", true)
    lane(active, "bolt.fill", "Active", "#37C871", true)
    lane(toReview, "eye", "To review", "#BB9AF7", true)
    lane(p0, "flame.fill", "P0 to-dos", "#FF5C5C", true)
    lane(waiting, "hourglass", "Waiting", "#4C8DFF", true)
    lane(paused, "pause.circle", "Paused", "#9AA0A6", true)
    lane(idle, "moon.zzz", "Idle", "#565F89", true)
    lane(background, "gearshape", "Background", "#565F89", true)
}.padding(6)
