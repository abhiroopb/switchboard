#!/usr/bin/env python3
"""Cell-anchored comments on a Google Sheet through the headless sanctioned Chrome profile.

Usage:
  python3 system/scripts/sheet-cell-comment.py list --sheet <url-or-id>
  python3 system/scripts/sheet-cell-comment.py post --sheet <url> --cell C9 --comment '<text>' [--expect '<cell text>']
  python3 system/scripts/sheet-cell-comment.py post --sheet <url> --spec comments.json [--dry-run] [--no-robot]

Why: the Drive API (and `switchboard drive comment-add`) cannot anchor a comment to a Sheets cell; an
API comment lands file-level. the owner's own feedback on a shared sheet is one comment per cell, anchored on that cell
(rule: system/prompts/external-replies.md Rule 15). This script opens `<sheet>/edit?gid=<gid>#gid=<gid>&range=<A1>`
in a background tab of the sanctioned profile through the shared launcher (one lease, MCP stopped on exit), checks
that the name box shows the cell and, with --expect, that the cell holds that text, opens the comment box
(Cmd+Option+M; Ctrl+Alt+M off macOS), types the comment (Shift+Enter between lines), submits (Cmd+Enter; Ctrl+Enter), then reads the comments back
through the Drive API and requires a `workbook-range` anchor quoting the cell text.

Rules it enforces: every comment opens with the bot prefix 🤖 (AGENTS.md, Bot Attribution Prefix) unless --no-robot
(only when the owner said "no robot" for these comments); a comment whose text already sits on the file is skipped, so a
rerun never duplicates; an uncertain submit is reported `unverified` and never retried; the script never edits a
cell, never resolves, replies to, or deletes a comment.

Spec file: a JSON list of {"cell": "C9", "comment": "...", "expect": "optional current cell text"}; "gid" per item
overrides --gid. JSON lines go to stdout; exit 0 only when every item is posted-and-verified or already posted.
Runbook: docs/runbooks/google-doc-ops.md, "Cell-anchored comments on Google Sheets".
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

PREFIX = '🤖'
CELL = re.compile(r'^[A-Za-z]{1,3}[0-9]{1,7}$')
DRAFT_STATE = '''() => {
  const box = [...document.querySelectorAll('[aria-label="Comment draft"]')].find(e => e.getBoundingClientRect().width > 0);
  if (box && document.activeElement !== box) box.focus();
  return {href: location.href, nameBox: document.querySelector('#t-name-box')?.value || '',
          cellText: (document.querySelector('#t-formula-bar-input')?.innerText || ''),
          focus: document.hasFocus(), active: document.activeElement?.className?.toString().slice(0, 60) || '',
          draft: !!box, draftText: box ? box.innerText : ''};
}'''
OPEN_DRAFTS = '''() => [...document.querySelectorAll('[aria-label="Comment draft"]')]
  .filter(e => e.getBoundingClientRect().width > 0 && e.innerText.trim()).length'''


def shortcut(action, platform=None):
    """The Sheets key for `action` ('comment' opens the box, 'submit' posts it): Cmd on macOS, Ctrl
    elsewhere (Windows and Linux Chrome use Ctrl+Alt+M and Ctrl+Enter)."""
    mod = 'Meta' if (platform or sys.platform) == 'darwin' else 'Control'
    return {'comment': f'{mod}+Alt+m', 'submit': f'{mod}+Enter'}[action]


def parse_sheet(ref):
    """Return (file_id, gid or None) from a Sheets URL or a bare file id."""
    match = re.search(r'/spreadsheets/d/([A-Za-z0-9_-]+)', ref)
    file_id = match.group(1) if match else ref.strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,}', file_id):
        raise ValueError(f'not a Google Sheets URL or file id: {ref}')
    gid = re.search(r'[#?&]gid=([0-9]+)', ref)
    return file_id, gid.group(1) if gid else None


def deep_link(file_id, gid, cell):
    base = f'https://docs.google.com/spreadsheets/d/{file_id}/edit'
    if gid:
        return f'{base}?gid={gid}#gid={gid}&range={cell.upper()}'
    return f'{base}#range={cell.upper()}'


def with_prefix(text, robot=True):
    text = text.strip()
    if not robot or text.startswith(PREFIX):
        return text
    return f'{PREFIX} {text}'


def normalize(text):
    return ' '.join((text or '').replace(' ', ' ').split())


def already_posted(comments, content, cell_text=None):
    """An anchored comment with this text (on this cell's text when known). A file-level twin does not count."""
    hit = anchored_match([c for c in comments if not c.get('deleted')], content, cell_text)
    return hit['comment'] if hit and hit['anchored'] and not hit.get('quote_mismatch') else None


def anchored_match(comments, content, cell_text=None):
    """The comment with this content, a Sheets cell anchor, and (when known) the cell text quoted."""
    want = normalize(content)
    for c in comments:
        if normalize(c.get('content')) != want:
            continue
        try:
            anchor = json.loads(c.get('anchor') or '{}')
        except ValueError:
            anchor = {}
        quoted = (c.get('quoted_file_content') or c.get('quotedFileContent') or {}).get('value', '')
        if anchor.get('type') != 'workbook-range':
            return dict(comment=c, anchored=False, quoted=quoted)
        if cell_text is not None and normalize(quoted) != normalize(cell_text):
            return dict(comment=c, anchored=True, quoted=quoted, quote_mismatch=True)
        return dict(comment=c, anchored=True, quoted=quoted)
    return None


def load_items(args):
    if args.spec:
        with open(args.spec) as fh:
            items = json.load(fh)
    else:
        if not (args.cell and args.comment):
            raise SystemExit('post needs --spec, or --cell and --comment')
        items = [dict(cell=args.cell, comment=args.comment, expect=args.expect)]
    out = []
    for item in items:
        cell = str(item.get('cell', '')).strip()
        if not CELL.match(cell):
            raise SystemExit(f'bad cell reference: {cell!r} (use A1 form, one cell)')
        if not str(item.get('comment', '')).strip():
            raise SystemExit(f'empty comment for {cell}')
        out.append(dict(cell=cell.upper(), comment=with_prefix(item['comment'], not args.no_robot),
                        expect=item.get('expect'), gid=str(item['gid']) if item.get('gid') else None))
    cells = [(i['gid'], i['cell']) for i in out]
    texts = [normalize(i['comment']) for i in out]
    if len(set(cells)) != len(cells) or len(set(texts)) != len(texts):
        raise SystemExit('each cell and each comment text may appear once per spec (verification matches on text)')
    return out


def list_comments(file_id):
    run = subprocess.run(['switchboard', 'drive', 'comments', '--file-id-or-url', file_id,
                          '--all-pages'], capture_output=True, text=True, timeout=120)
    if run.returncode:
        raise RuntimeError('comments failed: ' + (run.stderr or run.stdout)[:400])
    return json.loads(run.stdout).get('comments', [])


def log(event, **data):
    print(json.dumps(dict(event=event, **data), ensure_ascii=False), flush=True)


def browser_helpers():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'github-pr-images.py')
    spec = importlib.util.spec_from_file_location('github_pr_images', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def wait_state(g, p, page_id, check, seconds):
    end = time.monotonic() + seconds
    state = {}
    while time.monotonic() < end:
        try:
            state = g.evaluate(p, page_id, DRAFT_STATE)
        except RuntimeError as error:  # navigation mid-poll
            if 'context' not in str(error) and 'detached' not in str(error):
                raise
            state = {}
        if state and check(state):
            return state, True
        time.sleep(0.5)
    return state, False


def post_one(g, p, page_id, file_id, gid, item):
    tab = item['gid'] or gid
    g.tool(p, 'navigate_page', dict(pageId=page_id, type='url', url=deep_link(file_id, tab, item['cell'])))

    def on_cell(s):
        return s.get('nameBox', '').upper() == item['cell'] and (not tab or f'gid={tab}' in s.get('href', '').split('#')[-1])

    state, ok = wait_state(g, p, page_id, on_cell, 30)
    if ok:
        time.sleep(2)  # the name box can update before a tab switch finishes; read again once settled
        state, ok = wait_state(g, p, page_id, on_cell, 10)
    if not ok:
        return dict(status='skipped', reason=f"cell not selected (name box {state.get('nameBox')!r}); signed out or wrong tab?")
    cell_text = state.get('cellText', '').rstrip('\n')
    if item.get('expect') is not None and normalize(item['expect']) != normalize(cell_text):
        return dict(status='skipped', reason='cell text differs from --expect', cell_text=cell_text)
    if state.get('draft'):  # a draft box left open on another cell would take this comment
        g.tool(p, 'press_key', dict(pageId=page_id, key='Escape'))
        state, closed = wait_state(g, p, page_id, lambda s: not s.get('draft'), 5)
        if not closed:
            return dict(status='skipped', reason='a comment draft was already open; not typing into it', cell_text=cell_text)
    ok = False
    for _ in range(3):  # the name box updates before the grid takes keys; settle, then retry the shortcut
        time.sleep(2)
        g.tool(p, 'press_key', dict(pageId=page_id, key=shortcut('comment')))
        state, ok = wait_state(g, p, page_id, lambda s: s.get('draft'), 5)
        if ok:
            break
    if not ok and 'docoview' in state.get('active', ''):
        g.tool(p, 'press_key', dict(pageId=page_id, key='Escape'))
        return dict(status='skipped', cell_text=cell_text,
                    reason='cell already has a comment thread (Sheets keeps one per cell); reply only on the owner\'s ask')
    if not ok:
        return dict(status='skipped', reason='comment box did not open', cell_text=cell_text,
                    focus=state.get('focus'), active=state.get('active'))
    lines = item['comment'].split('\n')
    for index, line in enumerate(lines):
        if line:
            g.tool(p, 'type_text', dict(pageId=page_id, text=line))
        if index < len(lines) - 1:
            g.tool(p, 'press_key', dict(pageId=page_id, key='Shift+Enter'))
    state, ok = wait_state(g, p, page_id, lambda s: normalize(s.get('draftText')) == normalize(item['comment']), 5)
    if not ok:
        g.tool(p, 'press_key', dict(pageId=page_id, key='Escape'))
        return dict(status='skipped', reason='typed draft did not match; draft discarded', draft=state.get('draftText'))
    g.tool(p, 'press_key', dict(pageId=page_id, key=shortcut('submit')))
    time.sleep(2)
    pending = g.evaluate(p, page_id, OPEN_DRAFTS)
    return dict(status='submitted', cell_text=cell_text, draft_left_open=bool(pending))


def cmd_list(args):
    file_id, _ = parse_sheet(args.sheet)
    for c in list_comments(file_id):
        quoted = (c.get('quoted_file_content') or {}).get('value', '')
        log('comment', id=c.get('id'), author=(c.get('author') or {}).get('displayName'), resolved=c.get('resolved'),
            anchor=c.get('anchor'), quoted=quoted, content=c.get('content'), replies=len(c.get('replies') or []))
    return 0


def cmd_post(args):
    file_id, url_gid = parse_sheet(args.sheet)
    gid = args.gid or url_gid
    items = load_items(args)
    existing = list_comments(file_id)
    todo = []
    for item in items:
        hit = already_posted(existing, item['comment'], item.get('expect'))
        if hit:
            log('already_posted', cell=item['cell'], comment_id=hit.get('id'))
        else:
            todo.append(item)
    if args.dry_run or not todo:
        for item in todo:
            log('plan', cell=item['cell'], url=deep_link(file_id, item['gid'] or gid, item['cell']), comment=item['comment'])
        return 0
    g = browser_helpers()
    m = g.m
    p = m.Proxy(m.SESSIONS, m.amp_owner(m.processes(), os.getpid()))
    results = {}
    try:
        for attempt in range(args.lease_wait // 15):
            if p.lease.acquire():
                break
            log('lease_busy', attempt=attempt + 1, holder=m.read_holder(m.SESSIONS))
            time.sleep(15)
        else:
            raise RuntimeError('Profile lease busy after bounded wait')
        p.start_child()
        g.rpc(p, 'initialize', dict(protocolVersion='2024-11-05', capabilities={},
                                    clientInfo=dict(name='switchboard-sheet-cell-comment', version='1')))
        p.send(dict(jsonrpc='2.0', method='notifications/initialized'))
        existing = list_comments(file_id)  # another run may have posted while this one waited for the lease
        for item in list(todo):
            hit = already_posted(existing, item['comment'], item.get('expect'))
            if hit:
                log('already_posted', cell=item['cell'], comment_id=hit.get('id'), after_lease=True)
                todo.remove(item)
        if not todo:
            return 0
        first = todo[0]
        opened = g.text_of(g.tool(p, 'new_page', dict(url=deep_link(file_id, first['gid'] or gid, first['cell']),
                                                         background=True)))
        page_id = max(g.page_ids(opened))
        if not gid and any(not i['gid'] for i in todo):
            # Sheets rewrites a gid-less #range link to the first tab's #gid=…, dropping the range; learn that gid.
            state, ok = wait_state(g, p, page_id, lambda s: re.search(r'[#?&]gid=[0-9]+', s.get('href', '')), 30)
            if not ok:
                raise RuntimeError('could not read the default tab gid; pass --gid or a URL with gid=')
            gid = re.search(r'[#?&]gid=([0-9]+)', state['href']).group(1)
            log('default_tab', gid=gid)
        for item in todo:
            try:
                results[item['cell'] + item['comment']] = post_one(g, p, page_id, file_id, gid, item)
            except Exception as error:  # outcome unknown: never retry this item
                results[item['cell'] + item['comment']] = dict(status='unverified', reason=str(error)[:300])
            log('browser', cell=item['cell'], **results[item['cell'] + item['comment']])
        try:
            g.close_automation_tabs(p)
        except Exception as error:  # cleanup must not skip verification
            log('close_tabs_failed', error=str(error)[:200])
    finally:
        for step in (p.close_child, p.lease.file.close):
            try:
                step()
            except Exception as error:
                log('cleanup_failed', step=getattr(step, '__name__', 'close'), error=str(error)[:200])
        log('MCP_stopped_lease_released')
    code = 0
    comments = []
    for attempt in range(4):
        comments = list_comments(file_id)
        if all(anchored_match(comments, i['comment']) for i in todo
               if results.get(i['cell'] + i['comment'], {}).get('status') in ('submitted', 'unverified')):
            break
        time.sleep(3)
    for item in todo:
        result = results.get(item['cell'] + item['comment'], dict(status='skipped', reason='not attempted'))
        if result['status'] in ('submitted', 'unverified'):
            match = anchored_match(comments, item['comment'], result.get('cell_text'))
            if match and match['anchored'] and not match.get('quote_mismatch'):
                log('posted', cell=item['cell'], comment_id=match['comment'].get('id'), quoted=match['quoted'])
                continue
            log('not_verified', cell=item['cell'], found=bool(match), anchored=bool(match and match['anchored']),
                quoted=match and match['quoted'], note='inspect the sheet before any retry')
        else:
            log('skipped', cell=item['cell'], reason=result.get('reason'), cell_text=result.get('cell_text'))
        code = 1
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    lst = sub.add_parser('list', help='list comments with their anchors and quoted cell text')
    lst.add_argument('--sheet', required=True)
    post = sub.add_parser('post', help='post cell-anchored comments')
    post.add_argument('--sheet', required=True, help='Sheets URL (its gid picks the tab) or file id')
    post.add_argument('--gid', help='tab gid when the URL has none')
    post.add_argument('--spec', help='JSON list of {cell, comment, expect?, gid?}')
    post.add_argument('--cell')
    post.add_argument('--comment')
    post.add_argument('--expect', help='exact current cell text; the comment is skipped when the cell differs')
    post.add_argument('--no-robot', action='store_true', help='omit the 🤖 prefix (only on the owner\'s "no robot")')
    post.add_argument('--dry-run', action='store_true')
    post.add_argument('--lease-wait', type=int, default=900)
    args = parser.parse_args()
    sys.exit(cmd_list(args) if args.cmd == 'list' else cmd_post(args))


if __name__ == '__main__':
    main()
