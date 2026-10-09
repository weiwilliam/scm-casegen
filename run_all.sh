#!/usr/bin/env bash
#
# Drive the per-case-date steps end to end.
#
#   cp casegen.env.example casegen.env && $EDITOR casegen.env
#   ./run_all.sh [--from STEP] [--to STEP] [--dry-run]
#
# Steps: download, ics, assemble, subset, locations, cases, namelists
#
# One-off prerequisites NOT covered here (see README §3.1-§3.2):
#   scripts/build_ufs_utils.sh      build chgres_cube + sfc_climo_gen
#   scripts/download_sfc_climo.sh   raw climatology sources (~5.2 GB)
#   qsub scripts/make_sfc_climo.pbs tiled climatology for this resolution
#
# The 'ics' step submits a PBS job and waits for it.
set -euo pipefail
cd "$(dirname "$0")"
REPO=$PWD
ALL=(download ics assemble subset locations cases namelists)
FROM=download; TO=namelists; DRY=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --from) FROM=$2; shift 2 ;;
    --to)   TO=$2; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

[[ -f casegen.env ]] || { echo "ERROR: no casegen.env (copy casegen.env.example)" >&2; exit 1; }
set -a; source casegen.env; set +a
export CASEGEN_REPO=$REPO
idx() { for i in "${!ALL[@]}"; do [[ ${ALL[$i]} == "$1" ]] && { echo "$i"; return; }; done; echo -1; }
F=$(idx "$FROM"); T=$(idx "$TO")
[[ $F -ge 0 && $T -ge 0 ]] || { echo "ERROR: bad step name" >&2; exit 1; }

run() { echo; echo "=== $1 ==="; shift; [[ $DRY -eq 1 ]] && { echo "  would run: $*"; return; }; "$@"; }

for i in $(seq "$F" "$T"); do
  case ${ALL[$i]} in
    download)  run download ./scripts/download_gdas.sh ;;
    ics)       run ics bash -c '
                 jid=$(qsub -A "$CASEGEN_ACCOUNT" -q "$CASEGEN_QUEUE" \
                            -l job_priority="$CASEGEN_PRIORITY" \
                            -v CASEGEN_ENV="$CASEGEN_REPO/casegen.env" \
                            "$CASEGEN_REPO/scripts/make_ics.pbs")
                 echo "  submitted $jid"
                 until qstat -f "$jid" 2>/dev/null | grep -q "job_state"; do sleep 5; done
                 while qstat -f "$jid" 2>/dev/null | grep -q "job_state = [QR]"; do sleep 20; done
                 echo "  finished $jid"' ;;
    assemble)  run assemble ./scripts/assemble_in_dir.sh ;;
    subset)    run subset "$CASEGEN_PYTHON" python/extract_forcing_subset.py \
                   -o "$CASEGEN_SUBSET_DIR" --margin 12 ;;
    locations) run locations "$CASEGEN_PYTHON" python/check_land_mask.py \
                   --policy require-land --write_csv "$CASEGEN_LOCATIONS_CSV" ;;
    cases)     run cases "$CASEGEN_PYTHON" python/gen_scm_cases.py ;;
    namelists) run namelists ./scripts/write_case_namelists.sh ;;
  esac
done
echo; echo "done: ${ALL[$F]} .. ${ALL[$T]}"
