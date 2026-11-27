// Shapes of two CoS dispatch commands that once failed, with invented text and
// paths. Shared by rm-guard-quotes.test.ts and claude-bridge-rm-trash.test.ts.

export const BS = '\x5c'
const WINPATH = ['C:', 'Users', 'Pat', 'AppData', 'Local', 'Temp', 'wmux', 'shot-1.png'].join(BS)
export const GREP = `2>&1 | grep -o '"message": "[^"]*"'`
const BRIEF_LINES = [
  'Workstream: example-notes',
  "Browser: signed-in the owner's example sessions",
  '',
  "The owner's ask, verbatim:",
  "'I used to sync my reading list (it would mark articles as read). The sync broke so now it's not working.",
  "Here's what I need from you:",
  "- In the reader I'd like to see my saved list (I have other lists as well). All my lists drop articles I've read.",
  "Feel free to take control of my computer.'",
  '',
  "- Lists: https://lists.example/mylists/ (banner: 'The sync service disabled the list's API access, so refreshes can't run')",
  '- Reader web: https://web.reader.example/#/ (home)',
  `The owner's screenshots: ${WINPATH}`,
  '- Reader playback is out of scope.',
]
const BRIEF = BRIEF_LINES.join('\n')
export const DOUBLE_QUOTED = `cd /c/Users/Pat/switchboard && bash system/scripts/router-dispatch.sh --origin cos "${BRIEF}" ${GREP}`
export const HEREDOC = [
  `mkdir -p "/c/Users/Pat/scratch" && cat > "/c/Users/Pat/scratch/brief.txt" <<'EOF'`,
  BRIEF,
  'A "quoted" phrase and an odd apostrophe: don\'t',
  'EOF',
  `cd /c/Users/Pat/switchboard && bash system/scripts/router-dispatch.sh --origin cos "$(cat /c/Users/Pat/scratch/brief.txt)" ${GREP}`,
].join('\n')
