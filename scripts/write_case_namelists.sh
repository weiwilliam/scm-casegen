#!/usr/bin/env bash
#
# Write one case_config namelist per generated case, and verify every
# case_name matches its filename.
#
#   scripts/write_case_namelists.sh [-d YYYYMMDDHH] [-r SCM_ROOT] [-p PREFIX]
#                                   [-c CRES] [--check-only]
#
# UFS_case_gen.py does NOT write these files. Writing them by hand is how a
# case ends up silently reading another station's data: a copy-pasted namelist
# kept `case_name = 'flux_bell_...'` while being named flux_whit_*.nml, so the
# "whit" run read bell's driver file. This script derives every case_name from
# the generated driver files, then re-reads what it wrote to confirm.
#
# Note on the forcing_type settings below: `thermo_forcing_type = 0` and
# `mom_forcing_type = 0` look wrong (valid values are 1/2/3) but are inert for
# DEPHY cases. `input_type = 1` routes through apply_forcing_DEPHY
# (scm_forcing.F90), which is driven by the driver file's global attributes.
# This matches the shipped fv3_model_point_noah.nml.
set -euo pipefail

DATE=${CASEGEN_DATE:-}
SCM_ROOT=${CASEGEN_SCM_ROOT:-}
CRES=${CASEGEN_CRES:-768}
PREFIX=flux
CHECK_ONLY=0

usage() { sed -n '2,19p' "$0" | sed 's/^# \?//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case $1 in
    -d|--date)     DATE=$2; shift 2 ;;
    -r|--scm_root) SCM_ROOT=$2; shift 2 ;;
    -p|--prefix)   PREFIX=$2; shift 2 ;;
    -c|--cres)     CRES=$2; shift 2 ;;
    --check-only)  CHECK_ONLY=1; shift ;;
    -h|--help)     usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done

[[ -n $DATE     ]] || { echo "ERROR: no date (set CASEGEN_DATE or pass -d)" >&2; exit 1; }
[[ -n $SCM_ROOT ]] || { echo "ERROR: no SCM root (set CASEGEN_SCM_ROOT or pass -r)" >&2; exit 1; }

CASE_IN=$SCM_ROOT/scm/data/processed_case_input
CASE_CFG=$SCM_ROOT/scm/etc/case_config
[[ -d $CASE_IN  ]] || { echo "ERROR: no $CASE_IN" >&2; exit 1; }
[[ -d $CASE_CFG ]] || { echo "ERROR: no $CASE_CFG" >&2; exit 1; }

shopt -s nullglob
drivers=("$CASE_IN/${PREFIX}_"*"_${DATE}_SCM_driver.nc")
(( ${#drivers[@]} )) || { echo "ERROR: no ${PREFIX}_*_${DATE}_SCM_driver.nc in $CASE_IN" >&2; exit 1; }

cases=()
for f in "${drivers[@]}"; do
  b=$(basename "$f"); cases+=("${b%_SCM_driver.nc}")
done

if [[ $CHECK_ONLY -eq 0 ]]; then
  for c in "${cases[@]}"; do
    cat > "$CASE_CFG/${c}.nml" <<EOF
\$case_config
case_name = '${c}',
thermo_forcing_type = 0,
model_ics=.true.,
mom_forcing_type = 0,
relax_time = 7200.0,
sfc_flux_spec = .false.,
sfc_type=1,
reference_profile_choice = 2,
input_type=1,
C_RES = ${CRES},
\$end
EOF
  done
  echo "wrote ${#cases[@]} namelists to $CASE_CFG"
fi

ok=0; bad=0
for c in "${cases[@]}"; do
  f=$CASE_CFG/${c}.nml
  if [[ ! -f $f ]]; then echo "  MISSING: ${c}.nml"; bad=$((bad + 1)); continue; fi
  got=$(sed -n "s/^case_name *= *'\(.*\)',*/\1/p" "$f")
  if [[ $got == "$c" ]]; then ok=$((ok + 1)); else
    echo "  MISMATCH: ${c}.nml declares case_name='${got}'"; bad=$((bad + 1)); fi
done
echo "verified: $ok matched, $bad mismatched"
[[ $bad -eq 0 ]] || exit 1
