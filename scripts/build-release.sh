#!/usr/bin/env bash
# Build the CMVideo source release artefacts.
#
#   dist/CMVideo-<ver>-source.zip
#   dist/CMVideo-<ver>-source.tar.gz
#   dist/SHA256SUMS.txt
#
# Uses `git archive` so .gitignored stuff (.venv, __pycache__, etc.)
# never leaks into the bundle. Reads the version from censor/version.py
# so there's only one place to bump.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "$HERE"

if [ ! -d .git ]; then
    echo "error: $HERE is not a git repo. Run 'git init' first." >&2
    exit 1
fi

# ----- read version from censor/version.py -----
VERSION="$(python3 - <<'PY'
import re, sys
src = open("censor/version.py").read()
m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src)
if not m:
    sys.exit("APP_VERSION not found in censor/version.py")
print(m.group(1))
PY
)"

NAME="CMVideo-${VERSION}-source"
DIST="$HERE/dist"
mkdir -p "$DIST"

echo "Building source release for v${VERSION}"
echo "Output:                                                  size"

# ----- zip -----
ZIP="$DIST/${NAME}.zip"
rm -f "$ZIP"
git archive --format=zip --prefix="${NAME}/" -o "$ZIP" HEAD
printf "  %-54s %s\n" "$(basename "$ZIP")" "$(du -h "$ZIP" | cut -f1)"

# ----- tar.gz -----
TGZ="$DIST/${NAME}.tar.gz"
rm -f "$TGZ"
git archive --format=tar.gz --prefix="${NAME}/" -o "$TGZ" HEAD
printf "  %-54s %s\n" "$(basename "$TGZ")" "$(du -h "$TGZ" | cut -f1)"

# ----- checksums -----
SUMS="$DIST/SHA256SUMS.txt"
(
    cd "$DIST"
    sha256sum "${NAME}.zip" "${NAME}.tar.gz" > "SHA256SUMS.txt"
)
printf "  %-54s %s\n" "$(basename "$SUMS")" "$(du -h "$SUMS" | cut -f1)"

echo
echo "Artefacts ready in $DIST"
echo
echo "Next:"
echo "  git tag v${VERSION}"
echo "  git push --tags"
echo "  gh release create v${VERSION} --prerelease \\"
echo "      --title \"CMVideo ${VERSION}\" \\"
echo "      --notes-file CHANGELOG.md \\"
echo "      \"$ZIP\" \"$TGZ\" \"$SUMS\""
