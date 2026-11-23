#!/usr/bin/env python3
"""GitHub PR images through the signed-in sanctioned Chrome profile (headless).

Usage:
  python3 system/scripts/github-pr-images.py check  --pr <pr-url>                 # is the profile signed in? exit 0 yes, 1 no
  python3 system/scripts/github-pr-images.py upload --pr <pr-url> <png> [<png> ...] # upload files, print user-attachments URLs
  python3 system/scripts/github-pr-images.py render --pr <pr-url>                 # do the body's <details> images load?

GitHub has no API for user-content attachments; they only upload through the web editor. This script drives the
headless sanctioned profile through the shared launcher (one lease, background tabs, MCP stopped on exit) and never
reads or types credentials. The files go through the new-comment composer's file input at the foot of the PR timeline
(the body editor is lazy-loaded; the composer yields the same repo-scoped URLs). The composer is cleared afterwards and
never submitted; the caller sets the final body with `gh pr edit --body-file`. Files must live under the git toplevel
of the cwd (the launcher exposes it as the MCP filesystem root). JSON lines go to stdout; `upload` also writes
`attachments.json` next to the first file. Runbook: docs/runbooks/chrome-mcp-leases.md, "GitHub sign-in".
"""
import argparse
import importlib.util
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ROOT = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True, check=True).stdout.strip()
LAUNCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chrome-mcp-session.py')
spec = importlib.util.spec_from_file_location('chrome_session', LAUNCHER)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
HANDLE = os.environ.get('SWITCHBOARD_GITHUB_LOGIN', '{{OWNER_USER}}')
ARTIFACTS = os.path.join(ROOT, '.amp', 'in', 'artifacts')
ids = itertools.count(1)


def log(event, **data):
    print(json.dumps(dict(time=datetime.now(ZoneInfo('America/Los_Angeles')).isoformat(timespec='seconds'), event=event, **data)), flush=True)


def rpc(p, method, params, timeout=120):
    ident = next(ids)
    p.send(dict(jsonrpc='2.0', id=ident, method=method, params=params))
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not m.matches(p.owner, m.processes()):
            raise RuntimeError('Amp owner exited')
        try:
            _, _, message = p.events.get(timeout=2)
        except queue.Empty:
            continue
        if message is None:
            raise RuntimeError('MCP connection closed')
        if message.get('id') == ident:
            if 'error' in message:
                raise RuntimeError(str(message['error']))
            result = message['result']
            if result.get('isError'):
                raise RuntimeError(str(result))
            return result
    raise TimeoutError(method)


def tool(p, name, arguments, timeout=120):
    return rpc(p, 'tools/call', dict(name=name, arguments=arguments), timeout)


def text_of(result):
    return '\n'.join(c.get('text', '') for c in result.get('content', []))


def page_ids(text):
    return [int(x) for x in re.findall(r'^([0-9]+):', text, re.M)]


def evaluate(p, page_id, fn, timeout=120):
    text = text_of(tool(p, 'evaluate_script', {'pageId': page_id, 'function': fn}, timeout))
    match = re.search(r'```(?:json)?\s*\n(.*?)\n```', text, re.S)
    if not match:
        raise RuntimeError('Unrecognized evaluate result: ' + text[:400])
    return json.loads(match.group(1))


STATE = '''() => ({host: location.hostname, path: location.pathname, title: document.title, login: document.querySelector('meta[name=user-login]')?.content || ''})'''

EDITOR_INFO = '''() => {
  const ta = document.querySelector('#new_comment_field');
  const form = ta?.closest('form');
  if (!form) return {form: false};
  ta.scrollIntoView(); ta.focus();
  const fi = form.querySelector('input[type=file]');
  if (fi) { fi.setAttribute('aria-label', 'SWITCHBOARD-PR-BODY-FILE-INPUT'); fi.style.opacity = '1'; fi.style.position = 'static'; fi.style.width = '200px'; fi.style.height = '24px'; fi.removeAttribute('hidden'); }
  return {form: true, textareaLen: (ta?.value || '').length, fileInput: !!fi};
}'''

TEXTAREA_VALUE = '''() => ({value: document.querySelector('#new_comment_field')?.value || ''})'''

CLEAR_COMPOSER = '''() => {
  const ta = document.querySelector('#new_comment_field');
  if (!ta) return {cleared: false};
  ta.value = ''; ta.dispatchEvent(new Event('input', {bubbles: true})); ta.dispatchEvent(new Event('change', {bubbles: true}));
  for (const store of [sessionStorage, localStorage]) { try { for (const k of Object.keys(store)) if (k.includes('new_comment_field') || k.includes(location.pathname)) store.removeItem(k); } catch (e) {} }
  return {cleared: true, composerLen: ta.value.length};
}'''

BODY_IMAGES = '''async () => {
  const body = document.querySelector('.js-comment-body, [data-testid="issue-body"], .markdown-body');
  const imgs = [...body.querySelectorAll('details img')];
  await Promise.all(imgs.map(i => i.complete ? null : new Promise(r => { i.onload = r; i.onerror = r; })));
  const first = body.querySelector('details');
  if (first) first.scrollIntoView();
  return imgs.map(i => ({alt: i.alt, w: i.naturalWidth, h: i.naturalHeight, src: i.src}));
}'''

ATTACHMENT = re.compile(r'https://github\.com/user-attachments/assets/[A-Za-z0-9-]+')


def shot(p, page_id, name):
    os.makedirs(ARTIFACTS, exist_ok=True)
    path = os.path.join(ARTIFACTS, name)
    try:
        tool(p, 'take_screenshot', dict(pageId=page_id, filePath=path))
        log('screenshot', path=path)
    except Exception as error:  # evidence, never a gate
        log('screenshot_failed', error=str(error)[:200])


def close_automation_tabs(p):
    for page in page_ids(text_of(tool(p, 'list_pages', {})))[1:]:
        tool(p, 'close_page', dict(pageId=page))
    log('automation_tabs_closed')


def find_uid(snapshot_text):
    for line in snapshot_text.splitlines():
        if 'SWITCHBOARD-PR-BODY-FILE-INPUT' in line:
            match = re.search(r'uid=(\S+)', line)
            if match:
                return match.group(1)
    return None


def signed_in(state, pr_path):
    return state['host'] == 'github.com' and state['login'] == HANDLE and state['path'] == pr_path


def run(args):
    pr_path = urlparse(args.pr).path.rstrip('/')
    files = [os.path.abspath(f) for f in args.files]
    for f in files:
        if not os.path.isfile(f):
            raise SystemExit(f'not a file: {f}')
    p = m.Proxy(m.SESSIONS, m.amp_owner(m.processes(), os.getpid()))
    attachments = {}
    code = 0
    try:
        for attempt in range(args.lease_wait // 15):
            if p.lease.acquire():
                break
            log('lease_busy', attempt=attempt + 1, holder=m.read_holder(m.SESSIONS))
            time.sleep(15)
        else:
            raise RuntimeError('Profile lease busy after bounded wait')
        log('lease_acquired', phase=args.phase, roots=p.filesystem_roots)
        p.start_child()
        rpc(p, 'initialize', dict(protocolVersion='2024-11-05', capabilities={}, clientInfo=dict(name='switchboard-github-pr-images', version='1')))
        p.send(dict(jsonrpc='2.0', method='notifications/initialized'))
        page_id = max(page_ids(text_of(tool(p, 'new_page', dict(url=args.pr, background=True)))))
        deadline = time.monotonic() + 60
        state = None
        while True:
            try:
                state = evaluate(p, page_id, STATE)
            except RuntimeError as error:  # SSO or redirect navigation mid-poll
                if 'context' not in str(error) and 'detached' not in str(error):
                    raise
                state = None
            if state:
                log('pr_state', **state, signed_in=signed_in(state, pr_path))
            if state and signed_in(state, pr_path):
                break
            if time.monotonic() > deadline:
                shot(p, page_id, f'github-pr-notsignedin-{int(time.time())}.png')
                log('result', phase=args.phase, signed_in=False, final=state)
                close_automation_tabs(p)
                return 1
            time.sleep(5)
        if args.phase == 'check':
            shot(p, page_id, f'github-pr-signedin-{int(time.time())}.png')
            log('result', phase='check', signed_in=True, login=state['login'], title=state['title'])
        elif args.phase == 'render':
            time.sleep(5)
            images = evaluate(p, page_id, BODY_IMAGES)
            shot(p, page_id, f'github-pr-body-details-{int(time.time())}.png')
            broken = [i for i in images if not i['w']]
            log('result', phase='render', images=images, broken=len(broken))
            code = 1 if broken or not images else 0
        else:
            info = {}
            for _ in range(10):
                time.sleep(1)
                info = evaluate(p, page_id, EDITOR_INFO)
                if info.get('form') and info.get('fileInput'):
                    break
            log('composer', **info)
            if not info.get('fileInput'):
                raise RuntimeError('No file input in the new-comment composer (not signed in, or locked conversation)')
            snap_path = os.path.join(ARTIFACTS, 'github-pr-composer-snapshot.txt')
            snap_text = text_of(tool(p, 'take_snapshot', dict(pageId=page_id, verbose=True, filePath=snap_path)))
            try:
                with open(snap_path) as fh:
                    snap_text = fh.read() or snap_text
            except OSError:
                pass
            uid = find_uid(snap_text)
            if not uid:
                raise RuntimeError('file input uid not found in snapshot')
            for path in files:
                name = os.path.basename(path)
                before = set(ATTACHMENT.findall(evaluate(p, page_id, TEXTAREA_VALUE)['value']))
                tool(p, 'upload_file', dict(pageId=page_id, uid=uid, filePaths=[path], includeSnapshot=False), timeout=180)
                log('upload_started', file=name)
                url = None
                end = time.monotonic() + 120
                while time.monotonic() < end and not url:
                    time.sleep(3)
                    new = [u for u in ATTACHMENT.findall(evaluate(p, page_id, TEXTAREA_VALUE)['value']) if u not in before]
                    url = new[0] if new else None
                if not url:
                    raise RuntimeError(f'No attachment URL for {name} within 120 s')
                attachments[name] = url
                log('attachment', file=name, url=url)
                with open(os.path.join(os.path.dirname(files[0]), 'attachments.json'), 'w') as fh:
                    json.dump(attachments, fh, indent=2)
            log('composer_cleared', **evaluate(p, page_id, CLEAR_COMPOSER))
            log('result', phase='upload', attachments=attachments)
        close_automation_tabs(p)
        return code
    finally:
        p.close_child()
        p.lease.file.close()
        log('MCP_stopped_lease_released', phase=args.phase, profile_chrome=len(m.profile_processes(m.processes())))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('phase', choices=['check', 'upload', 'render'])
    parser.add_argument('--pr', required=True, help='PR URL, e.g. https://github.com/acme/example-repo/pull/10001')
    parser.add_argument('--lease-wait', type=int, default=900, help='seconds to wait for the browser lease (default 900)')
    parser.add_argument('files', nargs='*', help='PNG files to upload (upload phase)')
    args = parser.parse_args()
    if args.phase == 'upload' and not args.files:
        parser.error('upload needs at least one file')
    sys.exit(run(args))


if __name__ == '__main__':
    main()
