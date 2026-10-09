#!/usr/bin/env bash
#
# Build chgres_cube and sfc_climo_gen from a UFS_UTILS checkout on Derecho.
#
#   source casegen.env
#   scripts/build_ufs_utils.sh [UFS_UTILS_DIR]
#
# Defaults to $CASEGEN_UFS_UTILS. Expects the checkout to already contain
# modulefiles/build.derecho.intel.lua (copy it from this repo's modulefiles/).
#
# UFS_UTILS ships no Derecho modulefile. The one here targets spack-stack 1.6.0;
# the piece that is easy to miss is
#   /lustre/desc1/scratch/epicufsrt/contrib/modulefiles_extra
# which supplies the libfabric/1.15.2.0 that stack-cray-mpich/8.1.25 requires.
# Without it the module load fails with a dependency error.
set -euo pipefail

[[ ${1:-} == -h || ${1:-} == --help ]] && { sed -n '2,16p' "$0" | sed 's/^# \?//'; exit 0; }

ROOT=${1:-${CASEGEN_UFS_UTILS:-}}
[[ -n $ROOT ]] || { echo "ERROR: no UFS_UTILS dir (set CASEGEN_UFS_UTILS or pass one)" >&2; exit 1; }
[[ -d $ROOT ]] || { echo "ERROR: not a directory: $ROOT" >&2; exit 1; }
[[ -f $ROOT/modulefiles/build.derecho.intel.lua ]] || {
  echo "ERROR: $ROOT/modulefiles/build.derecho.intel.lua not found." >&2
  echo "       cp modulefiles/build.derecho.intel.lua $ROOT/modulefiles/" >&2
  exit 1; }

export LMOD_TMOD_FIND_FIRST=yes
module reset
module use "$ROOT/modulefiles"
module load build.derecho.intel
module list

cd "$ROOT"
rm -rf build install
mkdir -p build && cd build
cmake .. -DCMAKE_INSTALL_PREFIX="$ROOT/install" -DBUILD_TESTING=OFF
make -j4 chgres_cube sfc_climo_gen
make install 2>/dev/null || cmake --install . 2>/dev/null || true

for x in chgres_cube sfc_climo_gen; do
  p=$ROOT/install/bin/$x
  [[ -x $p ]] && echo "  built $p" || echo "  MISSING $p"
done
