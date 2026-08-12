#!/usr/bin/env bash
# Download the Vosk small English model (~40 MB zip, 68 MB on disk) into models/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODEL="vosk-model-small-en-us-0.15"
DEST="$ROOT/models"
URL="https://alphacephei.com/vosk/models/$MODEL.zip"

# Check for an actual model file, not just the directory — sync tools can
# recreate the (gitignored) tree as empty dirs, which must not skip the fetch.
if [ -f "$DEST/$MODEL/am/final.mdl" ]; then
    echo "already present: $DEST/$MODEL"
    exit 0
fi
rm -rf "$DEST/$MODEL"

mkdir -p "$DEST"
echo "downloading $URL ..."
curl -fL --retry 3 -o "$DEST/$MODEL.zip" "$URL"
unzip -q -o "$DEST/$MODEL.zip" -d "$DEST"
rm "$DEST/$MODEL.zip"
echo "done: $DEST/$MODEL"
