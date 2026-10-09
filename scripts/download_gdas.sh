#!/usr/bin/env bash
#
# Download the GDAS analyses that drive an SCM case.
#
#   scripts/download_gdas.sh [-d YYYYMMDDHH] [-o DEST] [-l HOURS] [-i INTERVAL]
#                            [--with-atmanl] [--dry-run]
#
# Fetches, for every forcing time in the window:
#   atmf<HHH>.nc   <- gdas.<date>/<hh>/atmos/gdas.t<hh>z.atmf000.nc
#   sfcf<HHH>.nc   <- gdas.<date>/<hh>/atmos/gdas.t<hh>z.sfcf000.nc
# plus, once, the surface analysis used to build the initial conditions:
#   gdas.t<hh>z.sfcanl.nc
#
# Each forcing time is the *f000 analysis of its own cycle*, not a lead time
# from a single run, so the forcing is the best estimate at every time rather
# than a drifting forecast. A manifest recording that mapping is written
# alongside the files.
#
# gdas.t<hh>z.atmanl.nc (~14 GB) is only needed for UFS_case_gen
# forcing_method 1 or 3; with the default method 2 the atmospheric state comes
# from the history files. Pass --with-atmanl if you need it.
#
# Note: no tiled ICs exist on this bucket for any date — only Gaussian netCDF
# and GRIB2. That is why scripts/make_ics.pbs exists.
set -euo pipefail

BASE=https://noaa-gfs-bdp-pds.s3.amazonaws.com
DATE=${CASEGEN_DATE:-}
DEST=${CASEGEN_FORCING_DIR:-}
LENGTH=${CASEGEN_LENGTH_HOURS:-48}
INTERVAL=${CASEGEN_FORCING_INTERVAL:-6}
WITH_ATMANL=0
DRY=0

usage() { sed -n '2,24p' "$0" | sed 's/^# \?//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case $1 in
    -d|--date)     DATE=$2; shift 2 ;;
    -o|--out)      DEST=$2; shift 2 ;;
    -l|--length)   LENGTH=$2; shift 2 ;;
    -i|--interval) INTERVAL=$2; shift 2 ;;
    --with-atmanl) WITH_ATMANL=1; shift ;;
    --dry-run)     DRY=1; shift ;;
    -h|--help)     usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done

[[ -n $DATE ]] || { echo "ERROR: no date (set CASEGEN_DATE or pass -d)" >&2; exit 1; }
[[ -n $DEST ]] || { echo "ERROR: no destination (set CASEGEN_FORCING_DIR or pass -o)" >&2; exit 1; }
[[ ${#DATE} -eq 10 ]] || { echo "ERROR: date must be YYYYMMDDHH, got '$DATE'" >&2; exit 1; }

Y=${DATE:0:4}; M=${DATE:4:2}; D=${DATE:6:2}; H=${DATE:8:2}
START="$Y-$M-$D $H:00:00"
# Work in epoch seconds. GNU date parses a trailing "+NN" after a HH:MM:SS time
# as a timezone offset, so "... 00:00:00 +30 hours" is rejected as an invalid
# date once NN exceeds the +/-24 h a timezone may legally be.
START_EPOCH=$(date -u -d "$START" +%s)
MAN=$DEST/download_manifest.txt

echo "date      : $DATE  ($START UTC)"
echo "window    : 0 to +${LENGTH} h every ${INTERVAL} h"
echo "dest      : $DEST"
[[ $DRY -eq 1 ]] && echo "** dry run **"

mkdir -p "$DEST"
{
  echo "# scm-casegen download manifest"
  echo "# initial time : $START UTC"
  echo "# window       : 0..+${LENGTH} h every ${INTERVAL} h"
  echo "# bucket       : noaa-gfs-bdp-pds"
  echo "# files are numbered by hours since the initial time"
  echo "#"
  printf "# %-12s %-12s %-14s %s\n" atmf sfcf VALID source
} > "$MAN.tmp"

get() {   # get <url> <path>
  local url=$1 path=$2
  if [[ -s $path ]]; then echo "  have $(basename "$path")"; return; fi
  if [[ $DRY -eq 1 ]]; then echo "  would GET $url"; return; fi
  curl -sS --fail --retry 3 -o "$path" "$url" \
    && echo "  got  $(basename "$path") ($(du -h "$path" | cut -f1))"
}

for ((i = 0; i <= LENGTH; i += INTERVAL)); do
  e=$((START_EPOCH + i * 3600))
  d=$(date -u -d "@$e" +%Y%m%d)
  h=$(date -u -d "@$e" +%H)
  v=$(date -u -d "@$e" +%Y%m%d%H%MZ)
  n=$(printf %03d "$i")
  get "$BASE/gdas.${d}/${h}/atmos/gdas.t${h}z.atmf000.nc" "$DEST/atmf${n}.nc"
  get "$BASE/gdas.${d}/${h}/atmos/gdas.t${h}z.sfcf000.nc" "$DEST/sfcf${n}.nc"
  printf "  %-12s %-12s %-14s %s\n" "atmf${n}.nc" "sfcf${n}.nc" "$v" \
         "gdas.${d}/${h}/atmos/gdas.t${h}z.{atmf,sfcf}000.nc" >> "$MAN.tmp"
done

# surface analysis at the initial time -> initial conditions
get "$BASE/gdas.${Y}${M}${D}/${H}/atmos/gdas.t${H}z.sfcanl.nc" "$DEST/gdas.t${H}z.sfcanl.nc"
if [[ $WITH_ATMANL -eq 1 ]]; then
  get "$BASE/gdas.${Y}${M}${D}/${H}/atmos/gdas.t${H}z.atmanl.nc" "$DEST/gdas.t${H}z.atmanl.nc"
fi

[[ $DRY -eq 1 ]] || mv "$MAN.tmp" "$MAN"
[[ $DRY -eq 1 ]] && rm -f "$MAN.tmp"

echo
echo "total: $(du -sh "$DEST" 2>/dev/null | cut -f1)"
echo "manifest: $MAN"
