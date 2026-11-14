#!/bin/bash
# A 480p converter for another node (media/convert-workers.yaml) or any machine
# that can reach convert-server.py: claim a video, fetch it, convert it with
# convert-480p.sh's own encode (same settings, the English audio track), send
# the copy back, repeat. Nothing to do: wait and ask again.
#
#   SERVER   convert-server.py's address (default http://movies-convert-server:8080)
#   WORK     scratch space for one video and its copy (default /work)
#   THREADS  ffmpeg threads (default 2)
#   SCRIPT   convert-480p.sh, sourced for encode (default /scripts/convert-480p.sh)
set -u
SERVER=${SERVER:-http://movies-convert-server:8080}
WORK=${WORK:-/work}
SCRIPT=${SCRIPT:-/scripts/convert-480p.sh}
IDLE=${IDLE:-600}
# shellcheck source=convert-480p.sh
source "$SCRIPT"
mkdir -p "$WORK"

while true; do
    id=$(curl -fsS -X POST "$SERVER/claim") || { sleep 60; continue; }
    if [ -z "$id" ]; then
        sleep "$IDLE"
        continue
    fi
    rm -f "$WORK"/in "$WORK"/out.mp4
    start=$(date +%s)
    if ! curl -fsS -o "$WORK/in" "$SERVER/file/$id"; then
        echo "fetch failed: $id (the claim expires and the video is handed out again)"
        sleep 60
        continue
    fi
    if encode "$WORK/in" "$WORK/out.mp4" -map '0:s?' -c:s mov_text || encode "$WORK/in" "$WORK/out.mp4" -sn; then
        curl -fsS -X PUT -T "$WORK/out.mp4" "$SERVER/done/$id" >/dev/null &&
            echo "done: $id ($(($(date +%s) - start)) s, $(($(stat -c %s "$WORK/out.mp4") >> 20)) MB)"
    else
        curl -fsS -X POST "$SERVER/fail/$id" >/dev/null
        echo "failed: $id"
    fi
    rm -f "$WORK"/in "$WORK"/out.mp4
done
