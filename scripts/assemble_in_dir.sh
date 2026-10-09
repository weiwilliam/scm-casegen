#!/usr/bin/env bash
#
# Assemble the UFS_case_gen --in_dir from the chgres output plus the static grid.
#
#   source casegen.env && scripts/assemble_in_dir.sh
#
# sfc_data.tile*.nc are the freshly built, date-specific surface ICs.
# Everything else (grid, mosaic, orography, vertical coordinate) is static and
# symlinked from $CASEGEN_STATIC_GRID.
set -euo pipefail
: "${CASEGEN_IC_DIR:?source casegen.env first}"
: "${CASEGEN_STATIC_GRID:?source casegen.env first}"
: "${CASEGEN_CRES:?source casegen.env first}"

cd "$CASEGEN_IC_DIR"
for t in 1 2 3 4 5 6; do
  src=chgres_run/out.sfc.tile${t}.nc
  [ -f "$src" ] || { echo "ERROR: missing $src — did make_ics.pbs finish?" >&2; exit 1; }
  cp -f "$src" "sfc_data.tile${t}.nc"
  ln -sf "$CASEGEN_STATIC_GRID/C${CASEGEN_CRES}_grid.tile${t}.nc" "C${CASEGEN_CRES}_grid.tile${t}.nc"
  ln -sf "$CASEGEN_STATIC_GRID/oro_data.tile${t}.nc"              "oro_data.tile${t}.nc"
  for v in ls ss; do
    s=$CASEGEN_STATIC_GRID/oro_data_${v}.tile${t}.nc
    [ -e "$s" ] && ln -sf "$s" "oro_data_${v}.tile${t}.nc"
  done
done
ln -sf "$CASEGEN_STATIC_GRID/grid_spec.nc" grid_spec.nc
ln -sf "$CASEGEN_STATIC_GRID/gfs_ctrl.nc"  gfs_ctrl.nc

echo "assembled $CASEGEN_IC_DIR"
ls -la "$CASEGEN_IC_DIR" | grep -E "sfc_data|grid_spec|gfs_ctrl" | head
echo
echo "NEXT: verify the surface state matches the case date, e.g. compare tsfco"
echo "      against sfcanl tmpsfc at a station. The original failure was silent."
