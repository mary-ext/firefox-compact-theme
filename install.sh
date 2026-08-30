#!/usr/bin/env bash
# Usage: ./install.sh <profile-dir>
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHEET="$REPO/chrome/userChrome.css"
[ -f "$SHEET" ] || { echo "missing $SHEET" >&2; exit 1; }

if [ $# -ne 1 ]; then
  echo "usage: $0 <profile-dir>" >&2
  echo >&2
  
  echo "available profiles:" >&2
  found=false

  case "$(uname -s)" in
    Darwin)
      profile_roots=("$HOME/Library/Application Support/Firefox/Profiles")
      ;;
    Linux)
      profile_roots=(
        "$HOME/.mozilla/firefox"
        "$HOME/snap/firefox/common/.mozilla/firefox"
        "$HOME/.var/app/org.mozilla.firefox/.mozilla/firefox"
      )
      ;;
    *)
      profile_roots=()
      ;;
  esac
  for root in "${profile_roots[@]}"; do
    for profile in "$root"/*/; do
      [ -f "$profile/prefs.js" ] || continue
      printf '  %s\n' "${profile%/}" >&2
      found=true
    done
  done

  $found || echo "  none found" >&2
  exit 1
fi

PROFILE="$1"

[ -f "$PROFILE/prefs.js" ] || { echo "not a Firefox profile: $PROFILE" >&2; exit 1; }

echo "profile: $PROFILE"

mkdir -p "$PROFILE/chrome"
TARGET="$PROFILE/chrome/userChrome.css"

# Preserve an existing stylesheet.
if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
  BACKUP="$TARGET.backup.$(date +%Y%m%d%H%M%S)"
  mv "$TARGET" "$BACKUP"
  echo "existing userChrome.css moved to $(basename "$BACKUP")"
fi
ln -sfn "$SHEET" "$TARGET"
echo "linked  $TARGET -> $SHEET"

PREF='user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);'
if ! grep -qF "toolkit.legacyUserProfileCustomizations.stylesheets" "$PROFILE/user.js" 2>/dev/null; then
  printf '// Enable userChrome.css.\n%s\n' "$PREF" >> "$PROFILE/user.js"
  echo "pref    added to user.js"
else
  echo "pref    already present in user.js"
fi

echo
echo "Done."
