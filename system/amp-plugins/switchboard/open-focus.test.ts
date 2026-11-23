import { expect, test } from 'bun:test'
import { foregroundPageOpenReason } from '../switchboard-guardrails'

// Rule 9b: an agent opens an HTML page in the background and never steals focus.
test('a bare open of a page is flagged', () => {
  for (const cmd of [
    'open C:/Users/tester/Development/switchboard/workstreams/x/assets/compare/2026-11-03-page.html',
    'open "/tmp/My Page.HTM"',
    "open 'file://C:/Users/tester/Development/switchboard/outputs/reviews/page.html'",
    'open https://sites.example.com/owner-demo/',
    '/usr/bin/open /tmp/page.html',
    'cd /tmp && open page.html',
    'python3 build.py; open out/index.html',
    "bash -c 'open /tmp/page.html'",
    'open -a Safari /tmp/page.html',
    'FOO=1 open /tmp/page.html',
    'command open /tmp/page.html',
    'sudo open /tmp/page.html',
    'env open /tmp/page.html',
    'nohup open /tmp/page.html',
    'printf x | xargs open /tmp/page.html',
    'if true; then open /tmp/page.html; fi',
    '{ open /tmp/page.html; }',
    'open -F -n /tmp/page.html',
    // Chrome and Preview raise themselves even with -g.
    'open -g /tmp/page.html',
    'open -g "file:///tmp/page.html"',
    'open -ga Safari /tmp/page.html',
    'open --background /tmp/page.html',
    'open -a Safari -g /tmp/page.html',
    'command open -g /tmp/page.html',
    'if true; then open -g /tmp/page.html; fi',
    'open /tmp/image.png',
    'open -g ~/shots/desktop-1.PNG',
    'qlmanage -p /tmp/image.png',
  ]) expect(foregroundPageOpenReason(cmd)).not.toBeNull()
})

test('the cmux-tab opener, a non-page target, or an explicit opt-out passes', () => {
  for (const cmd of [
    'python3 system/scripts/image_compare.py open /tmp/page.html',
    'python3 system/scripts/image_compare.py open https://sites.example.com/owner-demo/',
    'qlmanage -t /tmp/image.png',
    'SWITCHBOARD_FOCUS_OK=1 qlmanage -p /tmp/image.png',
    'open obsidian://open?vault=switchboard&file=AGENTS',
    'open .',
    'openssl x509 -in cert.html',
    "echo 'open /tmp/page.html'",
    'rg "open /tmp/page.html" docs',
    'SWITCHBOARD_FOCUS_OK=1 open /tmp/page.html',
    'open -a TextEdit --args /tmp/page.html',
    'open -g /tmp/notes.txt',
    // A computed target cannot be judged at the command line; it passes.
    'open "$PAGE"',
  ]) expect(foregroundPageOpenReason(cmd)).toBeNull()
})

// The Linux and Windows openers raise the browser too.
test('xdg-open, gio open, wslview, and start of a page are flagged', () => {
  for (const cmd of [
    'xdg-open /tmp/page.html',
    'xdg-open https://example.com/report',
    'gio open file:///tmp/page.html',
    'wslview /mnt/c/Users/tester/page.html',
    'start "" C:\\Users\\me\\switchboard\\page.html',
    'start /tmp/page.html',
    'cmd //c start page.html',
    'cmd.exe /c start "" https://example.com/',
    'explorer.exe C:\\Users\\me\\shot.png',
    'cygstart /tmp/page.html',
    'cd /tmp && xdg-open page.html',
  ]) expect(foregroundPageOpenReason(cmd)).not.toBeNull()
})

test('other openers pass for a non-page target or an explicit opt-out', () => {
  for (const cmd of [
    'xdg-open .',
    'start .',
    'explorer.exe .',
    'npm start',
    'docker start web',
    'systemctl start nginx',
    'start code index.html',
    'start notepad C:\\x\\a.html',
    'start /b node server.js http://localhost:3000',
    'cmd /c start /b node srv.js --open http://localhost:3000',
    'SWITCHBOARD_FOCUS_OK=1 xdg-open /tmp/page.html',
    "echo 'xdg-open /tmp/page.html'",
    'python3 system/scripts/image_compare.py open /tmp/page.html',
    'xdg-open "$PAGE"',
  ]) expect(foregroundPageOpenReason(cmd)).toBeNull()
})
