#!/usr/bin/env python3
"""pm-os-board: the PM OS sidebar for wmux.

cmux on macOS renders PM OS lanes (Pinned, Needs attention, Active, Workers,
To review, P0 to-dos, Waiting, Paused, Idle, Background) through a custom
sidebar (system/cmux-sidebars/pm-os.swift). wmux 2.49 has no sidebar extension
API, so this script serves the same board as a local web page and the kit opens
it in a wmux browser pane next to the terminal. Every row action calls the
cmux shim (cmux-shim.py), so the sidecar, wmux's own rows and the board stay in
step. Nothing here runs on macOS.

Commands
  serve   [--host H --port P]        run the board (foreground)
  ensure  [--host H --port P]        start `serve` detached unless the port answers
  open    [--workspace ID] [--pane]  ensure, then open the board in a wmux browser
                                     surface (fallback: the workspace browser side panel)
  render  --format html|markdown|json [--push <surface>]
                                     print one snapshot; --push updates a wmux
                                     markdown surface (`cmux markdown open` fallback)
  url                                print the board URL
  state                              print the lane view JSON (same as `cmux sidebar lanes --json`)

Environment
  PM_OS_BOARD_HOST / PM_OS_BOARD_PORT   bind address (default 127.0.0.1:7471)
  PM_OS_BOARD_LOG                       log for the detached server (default ~/.pm-os/board.log)
  PM_OS_BOARD_POLL_MS                   page refresh interval (default 3000)
  PM_OS_PYTHON                          interpreter for the shim (default: this one)
  plus everything cmux-shim.py reads (WMUX_BIN, PM_OS_WMUX_SIDECAR, ...)
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import pm_os_agent  # noqa: E402  (thread_pointer: Amp link or Claude resume command; agent: the configured default)
SHIM = HERE / "cmux-shim.py"
BOARD_VERSION = "0.1.0"
DEFAULT_HOST = os.environ.get("PM_OS_BOARD_HOST") or "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("PM_OS_BOARD_PORT") or 7471)
REVIEW_HOLD = "#bb9af7"  # cmux "to review" purple; mark-read clears it, as the Swift sidebar does
SELECTED_TINT = "#4C8DFF"
UNREAD_AMBER = "#E0AF68"


class BoardError(Exception):
    pass


# ------------------------------------------------------------------- shim I/O
def shim(*args: str, stdin: str | None = None, timeout: float = 90) -> subprocess.CompletedProcess:
    cmd = [os.environ.get("PM_OS_PYTHON") or sys.executable, str(SHIM), *args]
    return subprocess.run(cmd, capture_output=True, text=True, input=stdin, timeout=timeout)


def shim_ok(*args: str, **kw: Any) -> str:
    result = shim(*args, **kw)
    if result.returncode:
        raise BoardError((result.stderr or result.stdout or f"cmux {' '.join(args)} failed").strip())
    return result.stdout


def lane_view() -> dict[str, Any]:
    return json.loads(shim_ok("sidebar", "lanes", "--json"))


def load_shim_module():
    """Import cmux-shim.py (hyphenated file) for its Wmux runner."""
    spec = importlib.util.spec_from_file_location("cmux_shim", SHIM)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------- actions
def find_row(view: dict[str, Any], wid: str) -> dict[str, Any] | None:
    for lane in view["lanes"]:
        for row in lane["workspaces"]:
            if row["id"] == wid or row.get("ref") == wid:
                return row
    return None


def relaunch_command(thread: str | None) -> str:
    """The command that reopens a row's conversation. thread_pointer names a Claude
    session by its resume command and only for a real session id, so that text is
    safe to type; every other bound id is an Amp thread. A row with no bound
    conversation starts this machine's configured agent (PM_OS_AGENT, then
    router.yaml `agent:`), the same resolver every other PM OS launch uses."""
    if not thread:
        return pm_os_agent.agent()
    pointer = pm_os_agent.thread_pointer(thread)
    return pointer if pointer.startswith("claude --resume ") else f"amp threads continue {shlex.quote(thread)}"


def relaunch(row: dict[str, Any], select: bool) -> str:
    """Open a fresh workspace on the same title, cwd and Amp thread or Claude
    session, then close the old one. Mirrors the cmux context-menu Relaunch."""
    args = ["new-workspace", "--name", row.get("custom_title") or row.get("title") or "Untitled"]
    if row.get("current_directory"):
        args += ["--cwd", row["current_directory"]]
    args += ["--command", relaunch_command(row.get("thread")), "--focus", "true" if select else "false"]
    out = shim_ok(*args).strip()
    new_id = out.rsplit("(", 1)[-1].rstrip(")") if "(" in out else ""
    if row.get("thread") and new_id:
        shim("session-bind", "--workspace", new_id, "--thread", row["thread"])
    if row.get("pinned") and new_id:
        shim("workspace-action", "--action", "pin", "--workspace", new_id)
    shim_ok("close-workspace", "--workspace", row["id"])
    return out


def apply_action(payload: dict[str, Any]) -> dict[str, Any]:
    """One board action -> shim calls. Returns a JSON-able result; raises
    BoardError with the shim's message on failure."""
    action = str(payload.get("action") or "")
    wid = str(payload.get("workspace") or "")
    simple = {"pin", "unpin", "mark-unread", "move-top", "move-up", "move-down", "close-others",
              "set-color", "clear-color", "clear-name"}
    if action == "select":
        return {"ok": True, "out": shim_ok("select-workspace", "--workspace", wid)}
    if action == "close":
        return {"ok": True, "out": shim_ok("close-workspace", "--workspace", wid)}
    if action == "rename":
        title = str(payload.get("title") or "").strip()
        if not title:
            raise BoardError("rename needs a title")
        return {"ok": True, "out": shim_ok("rename-workspace", "--workspace", wid, "--title", title)}
    if action == "mark-read":
        view = lane_view()
        row = find_row(view, wid)
        out = shim_ok("workspace-action", "--action", "mark-read", "--workspace", wid)
        if row and (row.get("custom_color") or "").lower() == REVIEW_HOLD:
            out += shim_ok("workspace-action", "--action", "clear-color", "--workspace", wid)
        return {"ok": True, "out": out}
    if action in simple:
        args = ["workspace-action", "--action", action, "--workspace", wid]
        if action == "set-color":
            args += ["--color", str(payload.get("color") or "")]
        return {"ok": True, "out": shim_ok(*args)}
    if action == "copy-thread-url":
        row = find_row(lane_view(), wid)
        thread = (row or {}).get("thread")
        if not thread:
            raise BoardError("no Amp thread or Claude session is bound to this workspace")
        # Amp has a web link; a Claude session is named by the command that resumes it.
        url = thread if thread.startswith("http") else pm_os_agent.thread_pointer(thread)
        if not url:
            raise BoardError(f"{thread} is neither an Amp thread nor a Claude session")
        try:  # best effort: also put it on the clipboard through the portable helper (clip.exe on WSL)
            mux_sh = HERE.parent / "lib" / "mux.sh"
            subprocess.run(["bash", "-c", f'source "{mux_sh}" && mux_copy'], input=url, text=True, timeout=10,
                           capture_output=True)
        except Exception:
            pass
        return {"ok": True, "url": url}
    if action == "relaunch":
        row = find_row(lane_view(), wid)
        if not row:
            raise BoardError(f"workspace not found: {wid}")
        return {"ok": True, "out": relaunch(row, select=True)}
    if action == "relaunch-all":
        view = lane_view()
        outs = []
        for lane in view["lanes"]:
            for row in lane["workspaces"]:
                if row.get("thread"):
                    outs.append(relaunch(row, select=row.get("selected", False)))
        return {"ok": True, "out": "\n".join(outs), "count": len(outs)}
    if action == "reopen-last-closed":
        view = lane_view()
        closed = view.get("closed") or []
        if not closed:
            raise BoardError("no closed workspace recorded")
        last = closed[-1]
        args = ["new-workspace", "--name", last.get("title") or "Untitled", "--focus", "true"]
        if last.get("cwd"):
            args += ["--cwd", last["cwd"]]
        args += ["--command", relaunch_command(last.get("thread"))]
        return {"ok": True, "out": shim_ok(*args)}
    if action == "new-workspace":
        title = str(payload.get("title") or "🆕 New").strip()
        args = ["new-workspace", "--name", title, "--focus", "true"]
        if payload.get("cwd"):
            args += ["--cwd", str(payload["cwd"])]
        if payload.get("command"):
            args += ["--command", str(payload["command"])]
        return {"ok": True, "out": shim_ok(*args)}
    if action == "reload":
        return {"ok": True, "out": shim_ok("sidebar", "reload")}
    raise BoardError(f"unknown action: {action}")


# ------------------------------------------------------------------- renderers
def render_markdown(view: dict[str, Any]) -> str:
    stamp = time.strftime("%a %H:%M")
    lines = [f"**{stamp} · {view['workspace_count']} ws · {view['unread_total']} unread**", ""]
    if view.get("directory"):
        lines += [f"`{view['directory']}`", ""]
    for lane in view["lanes"]:
        if not lane["count"]:
            continue
        lines.append(f"## {lane['label']} ({lane['count']})")
        for row in lane["workspaces"]:
            marks = ("⚡" if row["agent_state"] == "running" else "") + ("!" if row["lane"] == "attention" else "")
            unread = f" **[{row['unread']}]**" if row["unread"] else ""
            sel = "**" if row.get("selected") else ""
            lines.append(f"- {sel}{row['icon']} {row['label']}{sel}{(' ' + marks) if marks else ''}{unread}  `{row['ref']}`")
        lines.append("")
    closed = view.get("closed") or []
    if closed:
        lines.append(f"_last closed: {closed[-1].get('title', '')}_")
    return "\n".join(lines).rstrip() + "\n"


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>PM OS board</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#1a1b26;--panel:#1f2130;--fg:#c0caf5;--dim:#9aa5ce;--mute:#565f89;--sel:__SELECTED__;--amber:__AMBER__;--line:#2a2e42}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:12.5px/1.35 -apple-system,"Segoe UI",system-ui,sans-serif}
#hdr{position:sticky;top:0;background:var(--bg);padding:8px 10px 6px;border-bottom:1px solid var(--line);z-index:2}
#hdr .l1{display:flex;align-items:center;gap:6px;font-weight:600}#hdr .l1 .unread{color:var(--amber)}#hdr .l1 .sp{flex:1}
#hdr .dir{color:var(--mute);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
#hdr button{background:transparent;border:1px solid var(--line);color:var(--dim);border-radius:4px;padding:1px 6px;font-size:11px;cursor:pointer}
#hdr button:hover{border-color:var(--dim);color:var(--fg)}
.lane{margin:6px 0 2px}.lane h2{margin:0;padding:3px 10px;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;display:flex;align-items:center;gap:6px;color:var(--dim)}
.lane h2 .dot{width:8px;height:8px;border-radius:2px;display:inline-block}.lane h2 .n{margin-left:auto;background:var(--line);border-radius:8px;padding:0 6px;font-size:10px;color:var(--dim)}
.row{display:flex;align-items:center;gap:8px;padding:4px 8px 4px 10px;cursor:default;border-left:3px solid transparent;user-select:none}
.row:hover{background:rgba(255,255,255,.04)}.row.sel{background:rgba(76,141,255,.18);border-left-color:var(--sel)}
.row .ic{width:18px;text-align:center;font-size:14px}.row .tx{flex:1;min-width:0}.row .t{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word}
.row .s{color:var(--mute);font-size:10.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .m{color:var(--dim);font-size:11px}.row .m .bolt{color:#f7d774}.row .m .bang{color:#ffb3ac;font-weight:700}
.row .u{background:var(--amber);color:#1a1b26;border-radius:9px;padding:0 6px;font-size:10px;font-weight:700;min-width:18px;text-align:center}
.row .x{color:var(--mute);border:0;background:transparent;cursor:pointer;font-size:12px;padding:0 2px;visibility:hidden}.row:hover .x{visibility:visible}.row .x:hover{color:#ff5c5c}
#menu{position:fixed;background:var(--panel);border:1px solid var(--line);border-radius:6px;min-width:170px;padding:4px 0;box-shadow:0 6px 24px rgba(0,0,0,.45);display:none;z-index:9}
#menu div{padding:5px 12px;cursor:pointer}#menu div:hover{background:rgba(76,141,255,.25)}#menu hr{border:0;border-top:1px solid var(--line);margin:4px 0}
#err{position:fixed;bottom:8px;left:8px;right:8px;background:#3a1f24;color:#ffb3ac;border:1px solid #6a2f3a;border-radius:6px;padding:6px 8px;font-size:11px;display:none;white-space:pre-wrap}
#empty{padding:24px 10px;color:var(--mute);text-align:center}
</style></head><body>
<div id="hdr"><div class="l1"><span id="clock"></span><span>·</span><span id="ws"></span><span>·</span><span id="unread" class="unread"></span><span class="sp"></span>
<button title="New workspace" onclick="newWorkspace()">+</button><button title="Reopen last closed" onclick="act({action:'reopen-last-closed'})">↺</button><button title="Reload lanes" onclick="act({action:'reload'})">⟳</button></div>
<div class="dir" id="dir"></div></div>
<div id="lanes"></div><div id="menu"></div><div id="err"></div>
<script>
const POLL=__POLL__;let view=null,menuRow=null;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function clock(){const d=new Date();document.getElementById('clock').textContent=d.toLocaleDateString(undefined,{weekday:'short'})+' '+d.toTimeString().slice(0,5);}
async function refresh(){try{const r=await fetch('/api/state');if(!r.ok)throw new Error(await r.text());view=await r.json();draw();hideErr();}catch(e){showErr('board: '+e.message);}}
function draw(){document.getElementById('ws').textContent=view.workspace_count+' ws';document.getElementById('unread').textContent=view.unread_total+' unread';
document.getElementById('dir').textContent=view.directory||'';const root=document.getElementById('lanes');let h='';
for(const lane of view.lanes){if(!lane.count)continue;h+=`<div class="lane"><h2><span class="dot" style="background:${lane.tint}"></span>${esc(lane.label)}<span class="n">${lane.count}</span></h2>`;
for(const w of lane.workspaces){const marks=(w.agent_state==='running'?'<span class="bolt">⚡</span>':'')+(w.lane==='attention'?'<span class="bang">!</span>':'');
const tint=w.custom_color?`style="border-left-color:${esc(w.custom_color)}"`:'';
h+=`<div class="row${w.selected?' sel':''}" data-id="${esc(w.id)}" ${tint} onclick="act({action:'select',workspace:'${esc(w.id)}'})" oncontextmenu="menu(event,'${esc(w.id)}')">
<span class="ic">${esc(w.icon||'▫')}</span><span class="tx"><div class="t">${esc(w.label||w.clean_title||w.title)}</div>${w.description?`<div class="s">${esc(w.description)}</div>`:''}</span>
<span class="m">${marks}</span>${w.unread?`<span class="u">${w.unread}</span>`:''}<button class="x" title="Close" onclick="event.stopPropagation();closeWs('${esc(w.id)}','${esc(w.label||w.title)}')">✕</button></div>`;}
h+='</div>';}
root.innerHTML=h||'<div id="empty">no workspaces</div>';}
function rowById(id){for(const l of view.lanes)for(const w of l.workspaces)if(w.id===id)return w;return null;}
async function act(p){try{const r=await fetch('/api/action',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(p)});const j=await r.json();if(!r.ok||j.error)throw new Error(j.error||r.statusText);
if(j.url){try{await navigator.clipboard.writeText(j.url);}catch(_){prompt('Thread link',j.url);}}hideErr();await refresh();return j;}catch(e){showErr(e.message);}}
function closeWs(id,label){if(confirm('Close "'+label+'"?'))act({action:'close',workspace:id});}
function newWorkspace(){const t=prompt('New workspace title','🆕 New');if(t)act({action:'new-workspace',title:t,cwd:view&&view.directory||''});}
function menu(ev,id){ev.preventDefault();const w=rowById(id);if(!w)return;menuRow=w;const m=document.getElementById('menu');
const items=[[w.pinned?'Unpin':'Pin',()=>act({action:w.pinned?'unpin':'pin',workspace:id})],['Move to top',()=>act({action:'move-top',workspace:id})],
['Rename…',()=>{const t=prompt('Rename workspace',w.custom_title||w.title);if(t)act({action:'rename',workspace:id,title:t});}],
['Copy thread link',()=>act({action:'copy-thread-url',workspace:id})],null,['Mark read',()=>act({action:'mark-read',workspace:id})],['Mark unread',()=>act({action:'mark-unread',workspace:id})],null,
['Relaunch',()=>{if(confirm('Relaunch "'+(w.label||w.title)+'" on its Amp thread or Claude session?'))act({action:'relaunch',workspace:id});}],
['Relaunch all',()=>{if(confirm('Relaunch every workspace with a bound Amp thread or Claude session?'))act({action:'relaunch-all'});}],null,
['Close',()=>closeWs(id,w.label||w.title)],['Close others',()=>{if(confirm('Close every other workspace?'))act({action:'close-others',workspace:id});}]];
m.innerHTML=items.map((it,i)=>it?`<div data-i="${i}">${esc(it[0])}</div>`:'<hr>').join('');
m.querySelectorAll('div').forEach(d=>d.onclick=()=>{hideMenu();items[+d.dataset.i][1]();});
m.style.display='block';m.style.left=Math.min(ev.clientX,innerWidth-190)+'px';m.style.top=Math.min(ev.clientY,innerHeight-m.offsetHeight-8)+'px';}
function hideMenu(){document.getElementById('menu').style.display='none';}
function showErr(t){const e=document.getElementById('err');e.textContent=t;e.style.display='block';}function hideErr(){document.getElementById('err').style.display='none';}
addEventListener('click',hideMenu);addEventListener('keydown',e=>{if(e.key==='Escape')hideMenu();});
clock();setInterval(clock,15000);refresh();setInterval(refresh,POLL);
</script></body></html>
"""


def render_html() -> str:
    poll = os.environ.get("PM_OS_BOARD_POLL_MS") or "3000"
    return PAGE.replace("__POLL__", poll).replace("__SELECTED__", SELECTED_TINT).replace("__AMBER__", UNREAD_AMBER)


# ------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = f"pm-os-board/{BOARD_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet unless traced
        if os.environ.get("PM_OS_BOARD_TRACE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: str, ctype: str = "application/json; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._send(200, render_html(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, json.dumps(lane_view()))
            if path == "/api/markdown":
                return self._send(200, render_markdown(lane_view()), "text/markdown; charset=utf-8")
            if path == "/healthz":
                return self._send(200, json.dumps({"ok": True, "board": BOARD_VERSION}))
            return self._send(404, json.dumps({"error": "not found"}))
        except Exception as exc:  # the page shows the shim's message instead of dying
            return self._send(500, json.dumps({"error": str(exc)}))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/action":
            return self._send(404, json.dumps({"error": "not found"}))
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            return self._send(200, json.dumps(apply_action(payload)))
        except BoardError as exc:
            return self._send(400, json.dumps({"error": str(exc)}))
        except Exception as exc:
            return self._send(500, json.dumps({"error": str(exc)}))


def board_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/"


def port_live(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def serve(host: str, port: int) -> int:
    if not SHIM.exists():
        raise BoardError(f"cmux-shim.py not found next to this script: {SHIM}")
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"pm-os-board {BOARD_VERSION} on {board_url(host, port)} (shim {SHIM})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def log_path() -> Path:
    return Path(os.environ.get("PM_OS_BOARD_LOG") or (Path.home() / ".pm-os" / "board.log"))


def pid_path() -> Path:
    return log_path().with_suffix(".pid")


def ensure(host: str, port: int, wait: float = 8.0) -> str:
    if port_live(host, port):
        return board_url(host, port)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(log_path(), "ab") as log:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve", "--host", host, "--port", str(port)],
                                stdout=log, stderr=log, **kwargs)
    pid_path().write_text(f"{proc.pid}\n")
    del proc  # detached on purpose; `stop` uses the pid file
    deadline = time.time() + wait
    while time.time() < deadline:
        if port_live(host, port):
            return board_url(host, port)
        time.sleep(0.2)
    raise BoardError(f"board did not answer on {board_url(host, port)} within {wait:.0f}s; see {log_path()}")


def stop() -> str:
    """Stop the detached server started by `ensure` (pid file next to the log)."""
    path = pid_path()
    if not path.exists():
        return "no board pid file; nothing to stop"
    pid = int(path.read_text().strip() or 0)
    path.unlink(missing_ok=True)
    if not pid:
        return "empty board pid file; nothing to stop"
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return f"board pid {pid} was not running"
    return f"stopped board pid {pid}"


def open_in_wmux(url: str, workspace: str | None, pane: bool) -> str:
    """Browser surface in the workspace (a pane the owner can drag narrow and
    keep at the left), or the workspace browser side panel with --pane false."""
    wmux = load_shim_module().Wmux()
    if pane:
        args = ["new-surface", "--type", "browser"]
        if workspace:
            args += ["--workspace", workspace]
        created = wmux.json(args, timeout=20)
        sid = str((created or {}).get("surfaceId") or "") if isinstance(created, dict) else ""
        if sid:
            result = wmux.run(["browser", "open", url, "--surface", sid], timeout=20)
            if result.returncode == 0:
                return f"ok browser surface {sid} -> {url}"
    result = wmux.run(["open", url] + (["--workspace", workspace] if workspace else []), timeout=20)
    if result.returncode:
        raise BoardError((result.stderr or "wmux open failed").strip())
    return f"ok browser side panel -> {url}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pm-os-board", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "ensure", "open", "url"):
        p = sub.add_parser(name)
        p.add_argument("--host", default=DEFAULT_HOST)
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        if name == "open":
            p.add_argument("--workspace", default=os.environ.get("CMUX_WORKSPACE_ID") or None)
            p.add_argument("--pane", default="true", help="true: browser surface in the workspace; false: workspace browser side panel")
    r = sub.add_parser("render")
    r.add_argument("--format", choices=("html", "markdown", "json"), default="markdown")
    r.add_argument("--push", metavar="SURFACE", help="also update this wmux markdown surface (surface:N or id)")
    sub.add_parser("state")
    sub.add_parser("stop")
    ns = ap.parse_args(argv)
    try:
        if ns.cmd == "serve":
            return serve(ns.host, ns.port)
        if ns.cmd == "ensure":
            print(ensure(ns.host, ns.port))
            return 0
        if ns.cmd == "stop":
            print(stop())
            return 0
        if ns.cmd == "url":
            print(board_url(ns.host, ns.port))
            return 0
        if ns.cmd == "open":
            url = ensure(ns.host, ns.port)
            print(open_in_wmux(url, ns.workspace, str(ns.pane).lower() not in ("0", "false", "no")))
            return 0
        if ns.cmd == "state":
            print(json.dumps(lane_view(), indent=2))
            return 0
        if ns.cmd == "render":
            view = lane_view()
            body = render_html() if ns.format == "html" else json.dumps(view, indent=2) if ns.format == "json" else render_markdown(view)
            if ns.push:
                shim_ok("markdown", "set", ns.push, "--title", "PM OS board", stdin=body)
            sys.stdout.write(body)
            return 0
    except BoardError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    try:  # a console-less start must not open a window per child process (no_window.py)
        import no_window
        no_window.install()
    except ImportError:
        pass
    sys.exit(main(sys.argv[1:]))
