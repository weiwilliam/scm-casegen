# scm-casegen

Turn a GDAS analysis date into runnable CCPP-SCM cases at a list of station
locations, against **your own CCPP-SCM checkout**.

Built for the NYS Mesonet flux towers (C768/L127, 2022-08-15 00Z, 18 stations)
but nothing here is specific to that case: the station list, the date, the
resolution and the SCM repo are all configuration.

```bash
cp casegen.env.example casegen.env
$EDITOR casegen.env            # point CASEGEN_SCM_ROOT at your SCM checkout
source casegen.env
./run_all.sh                   # download -> ICs -> subset -> cases -> namelists
```

## Why this exists

`UFS_case_gen.py` does the hard part, but getting a correct case out of it needs
four things it does not do for you, each of which fails *silently*:

| | |
|---|---|
| **Date-matched surface ICs** | With `forcing_method=2` the atmosphere comes from the history files but soil and surface come from `--in_dir`. Point that at a stock regression-test IC set and you get a summer atmosphere over frozen, snow-covered soil. `check_IC_hist_surface_compatibility` compares only land/sea/ice *type*, never the date. |
| **Land-consistent coordinates** | Two independent nearest points are resolved, ~7 km apart. A coastal station that is water on *both* grids passes the built-in check and yields a lake column at a land tower. |
| **A regional forcing subset** | The C768/L127 history files are chunked one horizontal level per chunk, so reading one column re-inflates ~2.4 GB. Case generation goes from ~7 min/station to ~1 min for all 18. |
| **`case_config` namelists** | Not written by `UFS_case_gen.py` at all. Hand-copying them is how a case ends up silently reading another station's data. |

## Layout

```
casegen.env.example     all user paths and the case definition — the only file to edit
run_all.sh              driver for the per-date steps
scripts/
  build_ufs_utils.sh    one-off: build chgres_cube + sfc_climo_gen
  download_sfc_climo.sh one-off: raw climatology sources (~5.2 GB)
  make_sfc_climo.pbs    one-off per resolution: tiled climatology
  download_gdas.sh      per date: GDAS atmf/sfcf + sfcanl
  make_ics.pbs          per date: chgres_cube -> sfc_data.tile*.nc
  assemble_in_dir.sh    per date: ICs + static grid -> --in_dir
  write_case_namelists.sh  per date: case_config namelists, with verification
python/
  extract_forcing_subset.py  regional subset of the history files
  check_land_mask.py         resolve land-consistent station coordinates
  gen_scm_cases.py           call UFS_case_gen.py once per station
templates/              chgres and sfc_climo namelists (@PLACEHOLDER@ substituted)
modulefiles/            Derecho/Intel build environment for UFS_UTILS
data/                   station metadata + resolved locations
```

Everything below is the long form: what each step does, and the traps.

---

## 0. Pipeline at a glance

| # | Step | Command | `run_all.sh` step | Rerun when |
|---|---|---|---|---|
| 1 | Build UFS_UTILS | `scripts/build_ufs_utils.sh` | — | once, ever |
| 2 | Raw climatology sources | `scripts/download_sfc_climo.sh` | — | once, ever |
| 3 | Tiled climatology | `qsub scripts/make_sfc_climo.pbs` | — | once per resolution |
| 4 | Download GDAS | `scripts/download_gdas.sh` | `download` | new date |
| 5 | Surface ICs | `qsub scripts/make_ics.pbs` | `ics` | new date |
| 6 | Assemble `in_dir` | `scripts/assemble_in_dir.sh` | `assemble` | new date |
| 7 | Regional forcing subset | `python/extract_forcing_subset.py` | `subset` | new date |
| 8 | Land-consistent coordinates | `python/check_land_mask.py` | `locations` | new date / new sites |
| 9 | DEPHY case files | `python/gen_scm_cases.py` | `cases` | new date |
| 10 | `case_config` namelists | `scripts/write_case_namelists.sh` | `namelists` | new date |
| 11 | Build and run the SCM | `cmake`, `run_scm.py` | — | — |

Steps 1-3 are one-off. Steps 4-10 are what `./run_all.sh` drives; use
`--from`/`--to` to run part of the chain, and `--dry-run` to see the commands.

---

## 1. Environments

Three different environments are needed. Mixing them up is the most common
failure.

| Purpose | Set by |
|---|---|
| `UFS_case_gen.py`, `check_land_mask.py`, `extract_forcing_subset.py` | `CASEGEN_PYTHON` + `ESMFMKFILE` in `casegen.env` |
| Building SCM / UFS_UTILS | `modulefiles/build.derecho.intel.lua` |

`UFS_case_gen.py` imports `xesmf` at module scope and dies on import without
`ESMFMKFILE`. The `swei` conda env does **not** have `xesmf`; the
`scm_casegen` venv does **not** have `dask`.

---

## 2. Download the GDAS data

Public bucket, no credentials:

```bash
source casegen.env
./scripts/download_gdas.sh                 # or --dry-run to list first
./scripts/download_gdas.sh -d 2021071500   # a different date, ignoring the config
```

Writes `atmf<HHH>.nc` / `sfcf<HHH>.nc` for every forcing time plus
`gdas.t<hh>z.sfcanl.nc`, and a `download_manifest.txt` recording which cycle
each file came from. Existing files are skipped, so it is safe to re-run.

Sizes: `atmf` ~6.8 GB each, `sfcf` ~1.0 GB, `sfcanl` 331 MB. The nine pairs are
~70 GB.

`gdas.t00z.atmanl.nc` (14 GB) is **not needed** for `forcing_method=2` — the
atmospheric state comes from the history files. Download it only if you switch
to `forcing_method=1` or `3`.

> **There are no tiled ICs on this bucket.** `gdas.<date>/<hh>/atmos/` holds
> only Gaussian netCDF and GRIB2 — no `RESTART/`, no `*.tile*.nc`. NCAR RDA
> (`d084001`, `d083003`) is GRIB2 only. Hence step 3.

---

## 3. Build the initial conditions

With `forcing_method=2` (the default) `UFS_case_gen.py` splits its sources
(`UFS_case_gen.py:3708-3724`):

- **atmospheric state** ← `--forcing_dir`, the history files from step 2
- **surface / soil / orography** ← `--in_dir`, a tiled C768 IC set

Only `sfc_data.tile*.nc` is date-dependent. Grid and orography are static and
can be symlinked from the EPIC C768 set.

### 3.1 Build chgres_cube and sfc_climo_gen (one-off)

```bash
git clone --recursive --depth 1 --branch ufs_utils_1_12_3 \
    https://github.com/ufs-community/UFS_UTILS.git $CASEGEN_UFS_UTILS
cp modulefiles/build.derecho.intel.lua $CASEGEN_UFS_UTILS/modulefiles/
cp scripts/build_ufs_utils.sh $CASEGEN_UFS_UTILS/
cd $CASEGEN_UFS_UTILS && ./build_ufs_utils.sh   # -> install/bin/{chgres_cube,sfc_climo_gen}
```

UFS_UTILS ships no Derecho modulefile, so `modulefiles/build.derecho.intel.lua`
here was written by hand against spack-stack 1.6.0. The piece that is easy to miss is
`/lustre/desc1/scratch/epicufsrt/contrib/modulefiles_extra`, which supplies the
`libfabric/1.15.2.0` that `stack-cray-mpich/8.1.25` requires; without it the
module load fails.

### 3.2 Tiled surface climatology (one-off per resolution)

`chgres_cube` reads **eleven** `C768.*.tileN.nc` climatology fields from
`fix_dir_target_grid`, and those reads are **unconditional** — the
`*_from_climo` flags control whether the values are *used*, not whether the
files are read. No global 6-tile C768 set exists on Derecho, so build one:

```bash
./scripts/download_sfc_climo.sh            # ~5.2 GB; leaf_area_index NOT needed
qsub -A $CASEGEN_ACCOUNT -q $CASEGEN_QUEUE -l job_priority=$CASEGEN_PRIORITY \
     -v CASEGEN_ENV=$PWD/casegen.env scripts/make_sfc_climo.pbs
```

Output is 54 files (9 fields x 6 tiles), renamed by the job to
`C<CRES>.<field>.tile<N>.nc` in `$CASEGEN_CLIMO_DIR`.

### 3.3 Surface ICs for the case date

```bash
qsub -A $CASEGEN_ACCOUNT -q $CASEGEN_QUEUE -l job_priority=$CASEGEN_PRIORITY \
     -v CASEGEN_ENV=$PWD/casegen.env scripts/make_ics.pbs
```

24 tasks, ~20 s runtime. The job renders `templates/chgres_config.nml` with the
date and paths from `casegen.env`, writes it as `config.nml`, and symlinks it to
`fort.41`. Key settings:

```fortran
  convert_atm = .false.          ! only sfc_data is needed for forcing_method=2
  convert_sfc = .true.
  convert_nst = .true.
  input_type  = "gaussian_netcdf"
  sfc_files_input_grid = "gdas.t00z.sfcanl.nc"
  orog_files_target_grid = "C768_oro_data.tile1.nc", ...
  fix_dir_target_grid = ".../C768_sfc_climo"
  vgtyp_from_climo = .true.      ! leave all *_from_climo at .true.
```

Three traps, all of which cost a job each:

1. **chgres reads `./fort.41`**, not the file you name. Fortran `open` without
   `status='old'` *creates* an empty file, so a missing namelist fails at the
   *read*, not the open.
2. **`cres_target_grid` is parsed from the orography filename** by splitting on
   the literal `_oro_` (`program_setup.F90:292`). EPIC's files are
   `oro_data.tile1.nc`, which does not match; `ie = index(...)-1 = -1` slips
   past the `if (ie == 0)` guard and yields an empty prefix, producing the bogus
   path `NULL/.slope_type.tile1.nc`. The `grid/` subdirectory holds symlinks
   named `C768_oro_data.tile*.nc` to fix this.
3. **Never set `*_from_climo = .false.`** with `gaussian_netcdf` input in
   chgres 1.12.3 — it is broken. `vfrac` comes out **byte-identical to `stype`**
   (soil-type categories in the vegetation-fraction slot) and `shdmin`/`shdmax`
   are all zero.

### 3.4 Assemble `in_dir`

```bash
./scripts/assemble_in_dir.sh
```

Copies `out.sfc.tile*.nc` to `sfc_data.tile*.nc` and symlinks the static
`C768_grid.tile*.nc`, `oro_data*.tile*.nc`, `grid_spec.nc` and `gfs_ctrl.nc`
from the EPIC C768 directory.

**Verify before moving on** — compare a station's surface fields against the
GDAS analysis at the same point:

```python
# tsfco should match sfcanl tmpsfc; stc should match soilt1-4
```

The original failure was silent: a 21 degC August atmosphere sitting on 4 degC
frozen, snow-covered soil. `check_IC_hist_surface_compatibility` only compares
land/sea/ice *type*, never the date, so nothing warns you.

---

## 4. Regional forcing subset

```bash
$CASEGEN_PYTHON python/extract_forcing_subset.py \
    -o $CASEGEN_SUBSET_DIR --margin 12 --workers 18
```

Not optional in practice. The C768/L127 `atmf` files are chunked one full
horizontal level per chunk (1 x 1 x 1536 x 3072, deflated); a chunk is ~18.9 MB,
larger than the netCDF chunk cache, so reading one 127-level column re-inflates
~2.4 GB to return 127 floats. Against the full files case generation takes
**~7 min per station**; against the subset it is **~1 min for all 18**.
67 GB -> ~240 MB.

`--margin 12` grid points (~140 km) comfortably covers the relocations from
step 5.

---

## 5. Land-consistent coordinates

```bash
$CASEGEN_PYTHON python/check_land_mask.py \
    --policy require-land --write_csv $CASEGEN_LOCATIONS_CSV
```

This script imports `UFS_case_gen` from `$CASEGEN_SCM_ROOT/scm/etc/scripts` for
its grid-search helpers, so the SCM checkout must be configured (or passed with
`--scm_root`).

`UFS_case_gen` resolves **two independent nearest points** — C768 native for
surface/orography, quilted history for state/forcing — typically ~7 km apart, so
coastal sites can straddle a shoreline. Its built-in
`check_IC_hist_surface_compatibility` tests only whether the two *agree*, not
whether either is land, so a site that is water on **both** grids passes
silently and yields a lake column at a land flux tower.

For the 18 NY sites: FLUX_ONTA mismatched (caught), **FLUX_BURT and FLUX_SOUT
were water-on-both and silent**. Displacements: BURT 10.0 km, ONTA 10.3 km,
SOUT 17.3 km.

Do **not** patch the built-in check — leave it armed as an independent verifier.

> `slmsk` is 0=water / 1=land / **2=ice** whereas history `land` is 0/1, so
> sea-ice points always mismatch. Will bite on winter or high-latitude cases.

---

## 6. Generate the DEPHY case files

```bash
$CASEGEN_PYTHON python/gen_scm_cases.py                   # all sites
$CASEGEN_PYTHON python/gen_scm_cases.py --dry-run         # show the commands
$CASEGEN_PYTHON python/gen_scm_cases.py --scm_root ~/other-scm   # another checkout
```

Run it under PBS rather than on a login node. Output lands **inside the SCM
checkout you point at**, one DEPHY v1 file per case at
`$CASEGEN_SCM_ROOT/scm/data/processed_case_input/<stid>_<date>_SCM_driver.nc`,
because `UFS_case_gen.py` writes relative to its own location.

`--scm_root` (or `CASEGEN_SCM_ROOT`) locates `UFS_case_gen.py`; `--case_gen`
overrides it with an explicit path. Everything else defaults from `casegen.env`.

### 6.1 `case_config` namelists

`UFS_case_gen.py` does **not** write these; they are created by hand and are
easy to get wrong — the original `flux_whit` namelist carried
`case_name = 'flux_bell_...'`, so the "whit" run silently read bell's data.

```bash
./scripts/write_case_namelists.sh              # write, then verify
./scripts/write_case_namelists.sh --check-only # verify an existing set
```

The script derives every `case_name` from the generated driver files and then
re-reads what it wrote, so a filename and its `case_name` cannot disagree.


`thermo_forcing_type = 0` / `mom_forcing_type = 0` look wrong (valid values are
1/2/3) but are **inert** for DEPHY cases: `input_type = 1` routes through
`apply_forcing_DEPHY` (`scm_forcing.F90:369`), driven by the driver file's
global attributes. This matches the shipped `fv3_model_point_noah.nml`.

---

## 7. Build the SCM

Full detail in [`scm_build_changes.md`](scm_build_changes.md). Short version:

```bash
cd $CASEGEN_SCM_ROOT
mkdir -p build && cd build
rm -rf CMakeCache.txt CMakeFiles          # a stale cache pins the old source dir
cmake -DCCPP_SUITES=$CASEGEN_SUITE ../     # NOT ../src, and 'ALL' no longer works
make -j8
```

Two changes to `run_scm.py` are required for the new layout (both applied):

```diff
-DEFAULT_BIN_DIR = 'scm/bin'
+DEFAULT_BIN_DIR = 'build'

-SCM_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
+SCM_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
```

`realpath` (not `abspath`) resolves the symlink cmake creates in the build
directory, so the script works invoked either as `build/run_scm.py` or as
`scm/src/run_scm.py`.

---

## 8. Run

```bash
export LMOD_TMOD_FIND_FIRST=yes
module reset
module use $CASEGEN_SCM_ROOT/scm/etc/modules/derecho_intel
module load 2.0.0                 # NOT 1.6.0 - the SCM is built with ifx 2025.2.1
export HOST=derecho               # see below
export SCM_ROOT=$CASEGEN_SCM_ROOT
cd $SCM_ROOT/scm
./bin/run_scm.py -c flux_whit_$CASEGEN_DATE -s $CASEGEN_SUITE
```

> **`export HOST=derecho` matters on compute nodes.** `run_scm.py:39` disables
> the MPI launcher only when `$HOST` contains "derecho". That is true on login
> nodes but **not** on compute nodes (`dec1735...`), so it falls back to
> `mpirun -np 1`, which does not exist under cray-mpich — exit code 127.
> `--mpi_command ""` does *not* help: an empty string is falsy and re-triggers
> the default. Either set `HOST`, or pass `--mpi_command "mpiexec -n 1"`.

---

## 9. Verify

Two checks are worth running before trusting anything downstream.

**Did the ICs land on the right date?** Compare a station's surface fields
against the GDAS analysis at the same point — `tsfco` should match `sfcanl`
`tmpsfc`, and `stc` should match `soilt1-4`. The original failure here was a
21 degC August atmosphere over 4 degC frozen soil, and nothing warned about it.

**Did every run stay physical?** Screen the output before computing statistics:

| Symptom | Cause seen in practice |
|---|---|
| Skin temperature > 60 degC, `LE` near zero all day | Soil moisture initialised near the wilting point — transpiration shuts down and all available energy becomes sensible heat |
| Run truncated mid-forecast | netCDF fill in `time_inst` as well as the data; mask the time axis before use |
| Flat, near-zero `LE` at urban sites | No urban canopy in this configuration; IGBP 13 is treated as low-vegetation land |

Divergence is a continuum, not a binary: in the reference case the two diverged
sites were the two driest of eighteen (9.4 % and 10.3 % into their
plant-available soil-moisture range), with the next two at 12.7 % and 17.2 %
and spared mostly by cloud cover.

## 10. Adapting to a new date

1. Repeat [§2](#2-download-the-gdas-data) with the new date.
2. Repeat [§3.3](#33-surface-ics-for-the-case-date) and
   [§3.4](#34-assemble-in_dir) — edit `cycle_year/mon/day/hour` and
   `sfc_files_input_grid` in `chgres_run/config.nml`. §3.1 and §3.2 do **not**
   need repeating.
3. Repeat §4-§6 with the new date. Update `CASE_DATE` / `IN_DIR` /
   `FORCING_DIR` in `gen_flux_site_cases.py`, or pass them on the command line.
4. Re-run `check_land_mask.py` — the land mask is grid-static but **sea ice is
   not** (`slmsk = 2`), so a winter date can change which sites pass.
5. `make_scm_flux_figures.py --date <new> --suite <suite>`.

A different resolution additionally needs §3.2 rebuilt for that grid, and
`C_RES` changed in the `case_config` namelists.

---

## Reference: what lives where

Every path is set in `casegen.env`; nothing below is hardcoded in a script.

| Variable | What it holds |
|---|---|
| `CASEGEN_SCM_ROOT` | **your CCPP-SCM checkout** — `UFS_case_gen.py`, and where cases are written |
| `CASEGEN_FORCING_DIR` | raw GDAS `atmf`/`sfcf` + `sfcanl` |
| `CASEGEN_SUBSET_DIR` | regional subset of the above — what case generation reads |
| `CASEGEN_IC_DIR` | assembled `--in_dir`; `chgres_run/` and `grid/` live inside it |
| `CASEGEN_CLIMO_SRC` | raw surface-climatology sources (one-off) |
| `CASEGEN_CLIMO_DIR` | tiled `C<CRES>.*.tileN.nc` climatology (one-off per resolution) |
| `CASEGEN_STATIC_GRID` | static grid/mosaic/orography — **not** a source of `sfc_data` |
| `CASEGEN_UFS_UTILS` | UFS_UTILS checkout providing `chgres_cube`, `sfc_climo_gen` |
| `CASEGEN_SITES_CSV` | station list (`stid`, `lat [degrees]`, `lon [degrees]`) |
| `CASEGEN_LOCATIONS_CSV` | land-consistent coordinates from `check_land_mask.py` |
| `CASEGEN_PYTHON` | interpreter with `xesmf` + `netCDF4` (plus `ESMFMKFILE`) |

Generated cases land inside the SCM checkout, because `UFS_case_gen.py` writes
relative to its own location:

```
$CASEGEN_SCM_ROOT/scm/data/processed_case_input/<stid>_<date>_SCM_driver.nc
$CASEGEN_SCM_ROOT/scm/etc/case_config/<stid>_<date>.nml
```
