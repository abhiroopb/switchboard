#!/usr/bin/env bash
# Validate a Reveal.js deck directory: structure, leftover placeholders, and a
# live serve + fetch check so a broken deck never ships unnoticed.
#
# Usage: validate-presentation.sh <deck-dir>
# Exit codes: 0 = OK, 1 = validation failure, 2 = usage error.
set -euo pipefail

deck="${1:-}"
if [[ -z "$deck" || ! -d "$deck" ]]; then
  echo "usage: validate-presentation.sh <deck-dir>" >&2
  exit 2
fi
deck="$(cd "$deck" && pwd)"

fail=0
warn=0
note() { printf "  %-52s %s\n" "$1" "$2"; }

# ── Structure ────────────────────────────────────────────────────────
for f in index.html slides.md; do
  if [[ -f "$deck/$f" ]]; then note "$f" "✓"; else note "$f" "✗ missing"; fail=1; fi
done
[[ -f "$deck/theme.css" ]] && note "theme.css" "✓" || note "theme.css" "– (optional, not present)"

# ── Content checks ───────────────────────────────────────────────────
if [[ -f "$deck/index.html" ]]; then
  if grep -q 'reveal\.js@' "$deck/index.html"; then
    ver="$(grep -o 'reveal\.js@[0-9.]*' "$deck/index.html" | head -1)"
    note "pinned Reveal.js version" "✓ ($ver)"
  else
    note "pinned Reveal.js version" "✗ no pinned reveal.js@<version> found"; fail=1
  fi
  grep -q 'data-markdown' "$deck/index.html" \
    && note "markdown slide source wired" "✓" \
    || { note "markdown slide source wired" "✗ no data-markdown section"; fail=1; }

  # Durable navigation contract: forward is always → (keyboard). Mouse clicks
  # must NEVER advance the deck — clicks are
  # reserved for links and text selection.
  grep -q "navigationMode:[[:space:]]*['\"]linear['\"]" "$deck/index.html" \
    && note "linear navigation (navigationMode)" "✓" \
    || { note "linear navigation (navigationMode)" "✗ navigationMode: 'linear' missing — ↓ must never be required"; fail=1; }
  if grep -q "addEventListener('click'" "$deck/index.html" && grep -q 'Reveal\.next()' "$deck/index.html"; then
    note "clicks do not advance" "✗ click-to-advance handler present — remove it (clicks must never navigate)"; fail=1
  else
    note "clicks do not advance" "✓"
  fi

  # Editorial shell contract (only when the deck uses the editorial system).
  if grep -q 'fontsource-variable/inter' "$deck/index.html"; then
    note "editorial shell detected" "✓ (Inter Variable)"
    grep -q 'width:[[:space:]]*1600' "$deck/index.html" \
      && note "  1600×900 logical size" "✓" \
      || { note "  1600×900 logical size" "⚠ width: 1600 not found"; warn=1; }
    grep -q 'id="chrome"' "$deck/index.html" \
      && note "  fixed footer chrome" "✓" \
      || { note "  fixed footer chrome" "⚠ #chrome footer missing"; warn=1; }
    grep -q 'hide-chrome' "$deck/index.html" \
      && note "  hide-chrome state support" "✓" \
      || { note "  hide-chrome state support" "⚠ body.hide-chrome rule missing"; warn=1; }
  elif grep -q -- '--brand:' "$deck/theme.css" 2>/dev/null; then
    note "editorial theme without Inter import" "⚠ theme.css uses editorial tokens but index.html lacks the fontsource Inter Variable link"
    warn=1
  fi
fi

# Synchronized builds: markup that depends on a JS sync handler must ship with
# that handler in index.html, or the build renders frozen/incorrect.
if [[ -f "$deck/slides.md" && -f "$deck/index.html" ]]; then
  check_handler() { # <slides.md marker> <required index.html handler> <label>
    if grep -q "$1" "$deck/slides.md"; then
      if grep -q "$2" "$deck/index.html"; then
        note "$3" "✓"
      else
        note "$3" "✗ slides.md uses $1 but index.html lacks $2"; fail=1
      fi
    fi
  }
  check_handler 'tl-walk'   'syncTimelineWalk'        'timeline walk handler paired'
  check_handler 'sw-stage'  'syncSurfaceWalk'         'surface walk handler paired'
  check_handler 'data-bg='  'syncFragmentBackgrounds' 'fragment background handler paired'
  # Sources re-parenting: without it, .slide-sources lines are hidden by the
  # theme's in-section fallback (or collide with content on legacy fixed CSS).
  if grep -q 'class="slide-sources"' "$deck/slides.md" \
     && grep -q 'fontsource-variable/inter' "$deck/index.html" \
     && ! grep -q 'syncSlideSources' "$deck/index.html"; then
    note "slide-sources re-parenting" "⚠ .slide-sources used but index.html lacks syncSlideSources (lines may hide or collide)"; warn=1
  fi
fi

# Source footers must carry real links, not placeholders.
if [[ -f "$deck/slides.md" ]] && grep -q 'class="slide-sources"' "$deck/slides.md"; then
  if grep -q 'href="https://docs.google.com/document/d/REPLACE' "$deck/slides.md"; then
    note "source links" "✗ placeholder REPLACE href still present"; fail=1
  elif grep 'class="slide-sources"' "$deck/slides.md" | grep -qv 'href="http'; then
    note "source links" "✗ .slide-sources block without an http(s) href"; fail=1
  else
    note "source links" "✓ all .slide-sources carry real links"
  fi
fi

# Help standard: every editorial deck ends with a counted
# Help slide carrying exactly these three links, and the footer chrome repeats
# them on every slide and PDF page. Basic shells are exempt (utility decks).
# Personalize here: replace the three template URLs below with your own DM link,
# team channel, and resource hub, and use the same URLs in the deck templates.
if [[ -f "$deck/slides.md" && -f "$deck/index.html" ]] && grep -q 'id="chrome"' "$deck/index.html"; then
  # Personalize here: use your own DM link, team channel, and resource hub,
  # and the same URLs in templates/slides.md and templates/index.html.
  help_missing=0
  for url in 'https://example.slack.com/team/' \
             'https://example.slack.com/archives/CXXXXXXXXXX' \
             'https://example.com/team-resources/'; do
    grep -qF "$url" "$deck/slides.md" || { note "help slide" "✗ slides.md lacks Help link $url"; help_missing=1; }
    grep -qF "$url" "$deck/index.html" || { note "help footer" "✗ index.html footer chrome lacks Help link $url"; help_missing=1; }
  done
  if [[ $help_missing -eq 1 ]]; then
    fail=1
  elif grep -q 'data-section="Help"' "$deck/slides.md"; then
    note "help standard" "✓ Help slide + footer carry DM the owner · team channel · resource hub"
  else
    note "help standard" "⚠ three Help links present but no slide tagged data-section=\"Help\""; warn=1
  fi
fi

if grep -rn '{{[A-Z_]*}}' "$deck"/index.html "$deck"/slides.md 2>/dev/null | head -3 | grep -q .; then
  note "template placeholders" "✗ unresolved {{…}} placeholders remain"; fail=1
else
  note "template placeholders" "✓ none left"
fi

if [[ -f "$deck/slides.md" && ! -s "$deck/slides.md" ]]; then
  note "slides.md content" "✗ empty"; fail=1
fi

# Referenced local assets exist?
if [[ -f "$deck/slides.md" ]]; then
  missing_assets=0
  while IFS= read -r ref; do
    [[ "$ref" == */ ]] && continue  # directory mention (e.g. in a comment), not a file ref
    [[ -f "$deck/$ref" ]] || { note "asset: $ref" "✗ referenced but missing"; missing_assets=1; }
  done < <(grep -o 'assets/[^)"'\'' ]*' "$deck/slides.md" 2>/dev/null | sort -u)
  (( missing_assets )) && fail=1 || note "referenced assets" "✓"

  # One photo, one moment: editorial photographs must not repeat across the
  # deck (design-system rule). Screenshots outside assets/photos/ are exempt.
  dup_photos="$(grep -o 'assets/photos/[^)"'\'' ]*' "$deck/slides.md" 2>/dev/null | sort | uniq -d)"
  if [[ -n "$dup_photos" ]]; then
    while IFS= read -r p; do note "photo reused: $p" "✗ each photo may appear only once"; done <<< "$dup_photos"
    fail=1
  else
    note "photo uniqueness" "✓"
  fi
fi

# ── Serve + fetch ────────────────────────────────────────────────────
if [[ -f "$deck/index.html" && -f "$deck/slides.md" ]]; then
  port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
  python3 -m http.server "$port" --directory "$deck" --bind 127.0.0.1 >/dev/null 2>&1 &
  server_pid=$!
  trap 'kill "$server_pid" 2>/dev/null || true' EXIT
  sleep 1

  http_index="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/" || echo 000)"
  http_slides="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/slides.md" || echo 000)"
  [[ "$http_index" == 200 ]] && note "serves index.html (HTTP $http_index)" "✓" || { note "serves index.html (HTTP $http_index)" "✗"; fail=1; }
  [[ "$http_slides" == 200 ]] && note "serves slides.md (HTTP $http_slides)" "✓" || { note "serves slides.md (HTTP $http_slides)" "✗"; fail=1; }

  kill "$server_pid" 2>/dev/null || true
  trap - EXIT
fi

echo ""
if (( fail )); then
  echo "❌ Deck validation failed: $deck"
  exit 1
fi
if (( warn )); then
  echo "⚠️  Deck validates with warnings (see ⚠ lines above): $deck"
else
  echo "✅ Deck validates: $deck"
fi
echo "   Preview: python3 -m http.server 8000 --directory '$deck'  →  http://localhost:8000"
