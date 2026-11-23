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
# - The English audio track if there is one (a dual-audio file's first track is
#   often Japanese), else the first.
# - Text subtitles are kept; if the subtitles can't be converted (picture
#   subtitles, such as a Blu-ray's), the copy is made without them.
# - Originals are never changed or deleted.
# - Pictures, subtitles and metadata files are copied beside the copies.
# - One run at a time (a lock file in $DST), however the run was started.
# - Two videos with one name in one folder (X.mkv and X.mp4; the stems
#   compared ignoring ASCII case, as exFAT does): the first in byte order keeps
#   X.mp4, which is the copy this script already made of it, and each other
#   gets X.<its extension>.mp4 (X.mp4.mp4). A "same name:" line says so.
set -u
SRC=${SRC:-/movies/library}
DST=${DST:-/movies/480p}
SETTLE=${SETTLE:-600}
THREADS=${THREADS:-2}
PRESET=${PRESET:-medium}  # x264 preset: veryfast is about twice as fast, files ~20-30% bigger

audio_track() {  # audio_track <input>: "0:<index>" of an English audio track, or nothing
    local index
    index=$(ffprobe -v error -select_streams a -show_entries stream=index:stream_tags=language \
        -of csv=p=0 "$1" | awk -F, 'tolower($2) == "eng" || tolower($2) == "en" { print $1; exit }')
    [ -n "$index" ] && echo "0:$index"
}

encode() {  # encode <input> <output> [subtitle options...]
    local in=$1 out=$2 audio
    shift 2
    audio=$(audio_track "$in")
    nice -n 19 ffmpeg -nostdin -hide_banner -loglevel error -y -i "$in" \
        -map 0:v:0 -map "${audio:-0:a:0?}" "$@" \
        -vf "scale='trunc(ih*dar/2)*2':ih,setsar=1,scale=-2:'min(480,ih)',setsar=1" \
        -c:v libx264 -preset "${PRESET:-medium}" -crf 22 -pix_fmt yuv420p -threads "$THREADS" \
        -c:a aac -ac 2 -b:a 128k -movflags +faststart -f mp4 "$out"
}

declare -A OWNER  # "<folder>/<stem, ASCII lowercase>" -> the video that keeps <stem>.mp4

name_key() {  # name_key <rel>: the folder and the stem in ASCII lowercase (exFAT ignores case)
    local LC_ALL=C stem=${1##*/}
    stem=${stem%.*}
    if [[ $1 == */* ]]; then printf '%s/%s' "${1%/*}" "${stem,,}"; else printf '%s' "${stem,,}"; fi
}

claim_names() {  # claim_names <rel>...: the first of each name (in byte order) keeps it
    local LC_ALL=C rel key
    for rel in "$@"; do
        key=$(name_key "$rel")
        if [[ -z ${OWNER[$key]+set} || $rel < ${OWNER[$key]} ]]; then
            OWNER[$key]=$rel
        fi
    done
}

output_rel() {  # output_rel <rel>: the 480p copy's path under $DST (after claim_names)
    local key
    key=$(name_key "$1")
    if [[ ${OWNER[$key]-$1} == "$1" ]]; then printf '%s.mp4' "${1%.*}"; else printf '%s.mp4' "$1"; fi
}

# Sourced (tests/test_media.py): define the functions only.
[[ "${BASH_SOURCE[0]}" != "$0" ]] && return 0

mkdir -p "$SRC" "$DST"
# One run at a time, even a Job started by hand beside the CronJob's own
# (concurrencyPolicy only counts the CronJob's Jobs): two runs would write the
# same .part file. Both pods are on brick2000, so a local flock covers it.
exec 9>"$DST/.convert.lock"
if ! flock -n 9; then
    echo "another run is converting; nothing to do"
    exit 0
fi
converted=0 failed=0 waiting=0 present=0

rels=()
while IFS= read -r -d '' in; do
    rels+=("${in#"$SRC"/}")
done < <(find "$SRC" -type f ! -name '.*' \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.m4v' \
    -o -iname '*.avi' -o -iname '*.mov' -o -iname '*.webm' -o -iname '*.ts' -o -iname '*.wmv' -o -iname '*.mpg' -o -iname '*.mpeg' \) -print0 | LC_ALL=C sort -z)
claim_names "${rels[@]}"

for rel in "${rels[@]}"; do
    in="$SRC/$rel"
    out="$DST/$(output_rel "$rel")"
    owner=${OWNER[$(name_key "$rel")]}
    [[ $owner != "$rel" ]] && echo "same name: $rel -> ${out#"$DST"/} ($owner keeps ${owner%.*}.mp4)"
    if [ -e "$out" ]; then present=$((present + 1)); continue; fi
    [ -e "$out.failed" ] && continue
    # Claimed by a worker on another node (convert-server.py), within 6 hours.
    claim="$DST/.claims/$(printf '%s' "$rel" | sha1sum | cut -c1-40)"
    [ -n "$(find "$claim" -mmin -360 2>/dev/null)" ] && continue
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
done

# Pictures, subtitles and metadata beside the videos (poster.jpg, X.jpg, X.srt,
# info.json, X.nfo: homelab-context scripts/media-ingest ships them) follow to
# the same path under $DST, so the handhelds' copies keep them. Copied again
# when the original is newer; never converted.
sidecars=0
while IFS= read -r -d '' in; do
    out="$DST/${in#"$SRC"/}"
    [ -e "$out" ] && ! [ "$in" -nt "$out" ] && continue
    mkdir -p "$(dirname "$out")"
    cp -p "$in" "$out" && sidecars=$((sidecars + 1))
done < <(find "$SRC" -type f ! -name '.*' \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \
    -o -iname '*.srt' -o -iname '*.ass' -o -iname '*.vtt' -o -iname '*.nfo' -o -name 'info.json' \) -print0)

echo "summary: converted=$converted failed=$failed waiting=$waiting already=$present sidecars=$sidecars"
