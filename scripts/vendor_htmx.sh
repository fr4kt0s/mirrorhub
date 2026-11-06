#!/bin/sh
# Downloads the pinned HTMX build into the app's static dir. Run on a machine with internet,
# then commit the file. The running app never contacts a CDN.
set -eu

VERSION="2.0.4"
DEST="$(dirname "$0")/../src/mirrorhub/web/static/htmx.min.js"

curl -fsSL "https://unpkg.com/htmx.org@${VERSION}/dist/htmx.min.js" -o "$DEST"
echo "Saved htmx ${VERSION} to ${DEST}"
sha256sum "$DEST"
echo "Record this hash in your change log and verify it on future updates."