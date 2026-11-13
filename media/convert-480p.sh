#!/bin/bash
# Converts every video under $SRC to a 480p H.264/AAC MP4 under $DST, at the
# same relative path: the Retroid's screen is 640x480, so a 480p copy looks the
# same there at a quarter of the size. Run by media/movies-cronjob.yaml.
#
# - Done once: a copy that exists is skipped, so each run only does new files.
# - Whole or not at all: ffmpeg writes a dotted .part file, renamed when done.
# - A file still being copied in (changed in the last $SETTLE seconds) waits
#   for the next run.
# - A file ffmpeg can't convert gets a <name>.mp4.failed marker and is skipped
#   from then on; delete the marker to try again.
# - Text subtitles are kept; if the subtitles can't be converted (picture
#   subtitles, such as a Blu-ray's), the copy is made without them.
# - Originals are never changed or deleted.
set -u
SRC=${SRC:-/movies/library}
DST=${DST:-/movies/480p}
SETTLE=${SETTLE:-600}
THREADS=${THREADS:-2}

mkdir -p "$SRC" "$DST"
converted=0 failed=0 waiting=0 present=0

encode() {  # encode <input> <output> [subtitle options...]
    local in=$1 out=$2
    shift 2
    nice -n 19 ffmpeg -nostdin -hide_banner -loglevel error -y -i "$in" \
        -map 0:v:0 -map '0:a:0?' "$@" \
        -vf "scale='trunc(ih*dar/2)*2':ih,setsar=1,scale=-2:'min(480,ih)',setsar=1" \
        -c:v libx264 -preset medium -crf 22 -pix_fmt yuv420p -threads "$THREADS" \
        -c:a aac -ac 2 -b:a 128k -movflags +faststart -f mp4 "$out"
}

while IFS= read -r -d '' in; do
    rel=${in#"$SRC"/}
    out="$DST/${rel%.*}.mp4"
    if [ -e "$out" ]; then present=$((present + 1)); continue; fi
    [ -e "$out.failed" ] && continue
    if [ $(($(date +%s) - $(stat -c %Y "$in"))) -lt "$SETTLE" ]; then
        waiting=$((waiting + 1))
        continue
    fi
    mkdir -p "$(dirname "$out")"
    part="$(dirname "$out")/.$(basename "$out").part"
    echo "converting: $rel"
    start=$(date +%s)
    if encode "$in" "$part" -map '0:s?' -c:s mov_text || encode "$in" "$part" -sn; then
        mv -f "$part" "$out"
        converted=$((converted + 1))
        echo "done: $rel ($(($(date +%s) - start)) s, $(($(stat -c %s "$out") >> 20)) MB)"
    else
        rm -f "$part"
        : > "$out.failed"
        failed=$((failed + 1))
        echo "failed: $rel"
    fi
done < <(find "$SRC" -type f ! -name '.*' \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.m4v' \
    -o -iname '*.avi' -o -iname '*.mov' -o -iname '*.webm' -o -iname '*.ts' -o -iname '*.wmv' \) -print0 | sort -z)

echo "summary: converted=$converted failed=$failed waiting=$waiting already=$present"
