#!/usr/bin/env bash
#
# Download the raw surface-climatology source files that sfc_climo_gen needs.
#
#   scripts/download_sfc_climo.sh [-o DEST] [--dry-run]
#
# One-off per machine, not per case date. ~5.2 GB.
#
# chgres_cube reads ELEVEN tiled <CRES>.*.tileN.nc climatology fields from
# fix_dir_target_grid, and those reads are UNCONDITIONAL — the *_from_climo
# namelist flags control whether the values are *used*, not whether the files
# are opened (static_data.F90). No global 6-tile set exists on Derecho for
# C768, so these raw sources are regridded by scripts/make_sfc_climo.pbs.
#
# leaf_area_index.0.05.nc (4.5 GB) is deliberately NOT fetched: chgres 1.12.3
# never reads leaf area index from the static files, so it would be 4.5 GB of
# nothing. soil_color is fetched because sfc_climo_gen can emit it, though
# chgres 1.12.3 ignores that too.
set -euo pipefail

BASE=https://noaa-nws-global-pds.s3.amazonaws.com/fix/sfc_climo/20230925
DEST=${CASEGEN_CLIMO_SRC:-}
DRY=0

FILES=(
  facsf.1.0.nc
  substrate_temperature.gfs.0.5.nc
  slope_type.1.0.nc
  maximum_snow_albedo.0.05.nc
  soil_type.statsgo.0.05.nc
  vegetation_type.modis.igbp.0.05.nc
  vegetation_greenness.0.144.nc
  soil_color.clm.0.05.nc
  snowfree_albedo.4comp.0.05.nc      # 4.7 GB, the bulk of the download
)

while [[ $# -gt 0 ]]; do
  case $1 in
    -o|--out)  DEST=$2; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

[[ -n $DEST ]] || { echo "ERROR: no destination (set CASEGEN_CLIMO_SRC or pass -o)" >&2; exit 1; }
mkdir -p "$DEST"
echo "dest: $DEST"

for f in "${FILES[@]}"; do
  if [[ -s $DEST/$f ]]; then echo "  have $f"; continue; fi
  if [[ $DRY -eq 1 ]]; then echo "  would GET $f"; continue; fi
  curl -sS --fail --retry 3 -o "$DEST/$f" "$BASE/$f" \
    && echo "  got  $f ($(du -h "$DEST/$f" | cut -f1))"
done

echo
echo "total: $(du -sh "$DEST" 2>/dev/null | cut -f1)"
