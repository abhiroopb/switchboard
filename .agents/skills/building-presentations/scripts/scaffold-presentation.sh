#!/usr/bin/env bash
# Scaffold a new Reveal.js deck from the skill templates.
#
# Usage:
#   scaffold-presentation.sh <target-dir> [--title "Deck Title"] [--brand "Footer Label"]
#                            [--basic [--theme <name>]] [--force]
#
# Default mode is EDITORIAL: the full dark editorial shell (Inter Variable,
# 1600×900, fixed footer chrome, linear navigation, click-to-advance,
# count-up stats) with theme-editorial.css and a pattern-sampler slides.md.
#
# --basic scaffolds a plain Reveal.js shell using a built-in theme instead —
# for throwaway/utility decks only. Built-in themes (--theme, basic mode
# only): black white league beige night serif simple solarized blood moon
# dracula. Default: black.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATES="$SKILL_DIR/templates"

target=""
title=""
brand=""
theme="black"
mode="editorial"
theme_set=false
force=false

while (( $# > 0 )); do
  case "$1" in
    --title) title="$2"; shift ;;
    --brand) brand="$2"; shift ;;
    --theme) theme="$2"; theme_set=true; shift ;;
    --basic) mode="basic" ;;
    --force) force=true ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "Unknown flag: $1" >&2; exit 2 ;;
    *) if [[ -z "$target" ]]; then target="$1"; else echo "Unexpected argument: $1" >&2; exit 2; fi ;;
  esac
  shift
done

if [[ -z "$target" ]]; then
  echo "usage: scaffold-presentation.sh <target-dir> [--title \"Deck Title\"] [--brand \"Footer Label\"] [--basic [--theme <name>]] [--force]" >&2
  exit 2
fi

if [[ "$mode" == "editorial" && "$theme_set" == true ]]; then
  echo "--theme applies to --basic mode only; the editorial shell uses theme-editorial.css." >&2
  exit 2
fi

case "$theme" in
  white|black|league|beige|night|serif|simple|solarized|blood|moon|dracula) ;;
  *) echo "Unknown theme '$theme' (expected a Reveal.js built-in theme)" >&2; exit 2 ;;
esac

if [[ -e "$target" && -n "$(ls -A "$target" 2>/dev/null)" && "$force" != true ]]; then
  echo "Refusing to scaffold into non-empty directory: $target (use --force to override)" >&2
  exit 1
fi

if [[ -z "$title" ]]; then
  # Derive a title from the directory name: strip leading date, dashes → spaces, title case.
  base="$(basename "$target")"
  title="$(printf '%s' "$base" | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}-//' | tr '-' ' ' \
    | awk '{for (i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) substr($i,2)}1')"
fi

# Footer brand label: keep it short (product/team name, not the deck title).
if [[ -z "$brand" ]]; then
  brand="$title"
fi

today="$(date +%Y-%m-%d)"
mkdir -p "$target/assets"
[[ "$mode" == "editorial" ]] && mkdir -p "$target/assets/photos"

# python3 for placeholder substitution so titles with slashes/ampersands are safe.
render() { # render <template> <dest>
  TITLE="$title" BRAND="$brand" THEME="$theme" DATE="$today" python3 - "$1" "$2" <<'PY'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
for key in ("TITLE", "BRAND", "THEME", "DATE"):
    text = text.replace("{{%s}}" % key, os.environ.get(key, ""))
open(dst, "w", encoding="utf-8").write(text)
PY
}

if [[ "$mode" == "editorial" ]]; then
  render "$TEMPLATES/index.html" "$target/index.html"
  render "$TEMPLATES/slides.md" "$target/slides.md"
  cp "$TEMPLATES/theme-editorial.css" "$target/theme.css"
else
  render "$TEMPLATES/index-basic.html" "$target/index.html"
  render "$TEMPLATES/slides-basic.md" "$target/slides.md"
  cp "$TEMPLATES/theme-basic.css" "$target/theme.css"
fi

echo "✅ Scaffolded Reveal.js deck ($mode): $target"
echo "   title: $title"
if [[ "$mode" == "editorial" ]]; then
  echo "   brand label: $brand (footer chrome — pass --brand for a short product/team name)"
else
  echo "   theme: $theme"
fi
echo ""
echo "Next steps:"
echo "  1. Write your content in $target/slides.md"
if [[ "$mode" == "editorial" ]]; then
  echo "     (starter demonstrates the pattern library — see references/design-system.md)"
fi
echo "  2. Preview: python3 -m http.server 8000 --directory '$target'  →  http://localhost:8000"
echo "  3. Validate: bash '$SKILL_DIR/scripts/validate-presentation.sh' '$target'"
