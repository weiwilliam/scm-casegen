#!/usr/bin/env python
"""Generate CCPP-SCM cases at a list of station locations.

For every station in the sites CSV this driver calls UFS_case_gen.py from the
user's own CCPP-SCM checkout with that station's lon/lat, building a case named
<stid>_<YYYYMMDDHH> (e.g. flux_bell_2022081500).

The SCM checkout is given by --scm_root (or CASEGEN_SCM_ROOT); UFS_case_gen.py
is expected at <scm_root>/scm/etc/scripts/UFS_case_gen.py. It writes its output
to a path relative to its own location (../../data/processed_case_input) and
imports fv3_remap from its own directory, so it is always invoked with cwd set
to that script directory — i.e. cases land inside the SCM checkout you point at.

Examples
--------
  # all sites, configuration from casegen.env
  source casegen.env
  ./gen_scm_cases.py --locations $CASEGEN_LOCATIONS_CSV

  # a subset, showing the commands without running them
  ./gen_scm_cases.py --sites FLUX_BELL FLUX_QUEE --dry-run

  # a different SCM checkout
  ./gen_scm_cases.py --scm_root ~/my-ccpp-scm

  # pass extra flags straight through to UFS_case_gen.py
  ./gen_scm_cases.py -- --save_comp --geostrophic
"""

import argparse
import csv
import os
import subprocess
import sys

# Defaults ####################################################################

# Your CCPP-SCM checkout. UFS_case_gen.py is located inside it.
SCM_ROOT = os.environ.get('CASEGEN_SCM_ROOT', '')

METADATA_CSV = os.environ.get('CASEGEN_SITES_CSV', '')
LOCATIONS_CSV = os.environ.get('CASEGEN_LOCATIONS_CSV', '')

# Initial conditions for the case date. Build these with scripts/make_ics.pbs —
# do NOT point this at a stock UFS regression-test IC set, whose sfc_data is for
# an unrelated date. That failure is silent: UFS_case_gen's
# check_IC_hist_surface_compatibility compares only land/sea/ice type.
IN_DIR = os.environ.get('CASEGEN_IC_DIR', '')
GRID_DIR = IN_DIR

# UFS history files used for the forcing; prefer the regional subset.
FORCING_DIR = (os.environ.get('CASEGEN_SUBSET_DIR')
               or os.environ.get('CASEGEN_FORCING_DIR', ''))

CASE_DATE = os.environ.get('CASEGEN_DATE', '')

# CSV column names.
COL_STID = 'stid'
COL_LAT = 'lat [degrees]'
COL_LON = 'lon [degrees]'


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--csv', default=METADATA_CSV,
                        help='flux site metadata CSV (default: %(default)s)')
    parser.add_argument('-r', '--scm_root', default=SCM_ROOT,
                        help='CCPP-SCM checkout containing '
                             'scm/etc/scripts/UFS_case_gen.py')
    parser.add_argument('--case_gen',
                        help='explicit path to UFS_case_gen.py; overrides '
                             '--scm_root')
    parser.add_argument('-i', '--in_dir', default=IN_DIR,
                        help='UFS initial conditions directory')
    parser.add_argument('-g', '--grid_dir', default=GRID_DIR,
                        help='UFS supergrid ("fix") directory')
    parser.add_argument('-f', '--forcing_dir', default=FORCING_DIR,
                        help='UFS history file directory; its basename is '
                             'used as the initial time unless --date is given')
    parser.add_argument('-d', '--date', default=CASE_DATE or None,
                        help='initial time as YYYYMMDDHH or YYYYMMDDHHMMSS '
                             '(default: taken from the forcing_dir basename)')
    parser.add_argument('-s', '--sites', nargs='+', metavar='STID',
                        help='only process these station ids '
                             '(default: every site in the CSV)')
    parser.add_argument('--locations', metavar='CSV', default=LOCATIONS_CSV or None,
                        help='manifest from check_land_mask.py --write_csv. '
                             'Uses its scm_lon/scm_lat instead of the raw '
                             'station coordinates, so coastal stations land on '
                             'a point where the IC and history grids agree on '
                             'land. Displacements are echoed per station.')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the commands without running them')
    parser.add_argument('extra_args', nargs=argparse.REMAINDER,
                        help='additional arguments passed through to '
                             'UFS_case_gen.py (put them after a bare --)')
    return parser.parse_args()


def resolve_date(date, forcing_dir):
    """Return (YYYYMMDDHHMMSS, YYYYMMDDHH) for the case date."""
    if date is None:
        date = os.path.basename(os.path.normpath(forcing_dir))

    if not date.isdigit() or len(date) not in (10, 14):
        raise ValueError(
            'Cannot interpret {0!r} as an initial time; expected YYYYMMDDHH '
            'or YYYYMMDDHHMMSS. Use --date to set it explicitly.'.format(date))

    full = date.ljust(14, '0')
    return full, full[:10]


def read_locations(csv_file):
    """Read a resolved-location manifest -> {stid: (lon, lat, displacement_km)}.

    The manifest is produced by check_land_mask.py, which snaps coastal stations
    onto a point where the IC and history grids agree on land. Keeping this in a
    separate file (rather than editing the station metadata) preserves the true
    tower coordinates and leaves an auditable record of every displacement.
    """
    with open(csv_file, 'r') as fh:
        rows = list(csv.DictReader(fh))
    out = {}
    for row in rows:
        out[row['stid'].strip()] = (float(row['scm_lon']) % 360.0,
                                    float(row['scm_lat']),
                                    float(row.get('displacement_km', 0.0)))
    return out


def read_sites(csv_file, wanted):
    """Read the metadata CSV, returning [(stid, lon_deg_east, lat), ...]."""
    with open(csv_file, 'r') as fh:
        rows = list(csv.DictReader(fh))

    for col in (COL_STID, COL_LAT, COL_LON):
        if rows and col not in rows[0]:
            raise KeyError('Column {0!r} not found in {1}'.format(col, csv_file))

    sites = []
    for row in rows:
        stid = row[COL_STID].strip()
        if wanted and stid not in wanted:
            continue
        lat = float(row[COL_LAT])
        # UFS_case_gen.py wants longitude in [0, 360].
        lon = float(row[COL_LON]) % 360.0
        sites.append((stid, lon, lat))

    if wanted:
        missing = sorted(set(wanted) - {s[0] for s in sites})
        if missing:
            raise ValueError('Station id(s) not in {0}: {1}'.format(
                csv_file, ', '.join(missing)))

    return sites


def main():
    args = parse_args()

    # argparse.REMAINDER keeps the separating '--' when it is present.
    extra = [a for a in args.extra_args if a != '--']

    date_full, date_short = resolve_date(args.date, args.forcing_dir)
    sites = read_sites(args.csv, set(args.sites) if args.sites else None)

    if not sites:
        print('No sites to process.')
        return 0

    case_gen = args.case_gen
    if not case_gen:
        if not args.scm_root:
            raise SystemExit('ERROR: set CASEGEN_SCM_ROOT (source casegen.env) '
                             'or pass --scm_root /path/to/ccpp-scm')
        case_gen = os.path.join(args.scm_root, 'scm', 'etc', 'scripts',
                                'UFS_case_gen.py')
    case_gen = os.path.abspath(case_gen)
    if not os.path.isfile(case_gen):
        raise SystemExit(f'ERROR: no UFS_case_gen.py at {case_gen}')
    work_dir = os.path.dirname(case_gen)

    print('scm_root    : {0}'.format(args.scm_root or '(from --case_gen)'))
    print('case_gen    : {0}'.format(case_gen))
    print('in_dir      : {0}'.format(args.in_dir))
    print('grid_dir    : {0}'.format(args.grid_dir))
    print('forcing_dir : {0}'.format(args.forcing_dir))
    print('date        : {0}'.format(date_full))
    print('locations   : {0}'.format(args.locations or 'raw station coordinates'))
    print('sites       : {0}\n'.format(len(sites)))

    if args.locations and not os.path.isfile(args.locations):
        raise SystemExit(
            f'ERROR: locations manifest not found: {args.locations}\n'
            '       Generate it with:  check_land_mask.py --policy require-land '
            '--write_csv <path>\n'
            '       or pass --locations "" to use the raw station coordinates '
            '(not recommended:\n'
            '       coastal sites may land on water on both grids and fail '
            'silently).')
    locations = read_locations(args.locations) if args.locations else {}
    if locations:
        unknown = sorted({s[0] for s in sites} - set(locations))
        if unknown:
            raise SystemExit(
                'These stations are missing from {0}: {1}\nRe-run '
                'check_land_mask.py --write_csv so the manifest covers every '
                'station being generated.'.format(args.locations,
                                                  ', '.join(unknown)))

    failed = []
    for n, (stid, lon, lat) in enumerate(sites, start=1):
        note = ''
        if locations:
            lon, lat, moved = locations[stid]
            if moved > 0:
                note = '  [relocated {0:.1f} km for land-mask consistency]'.format(moved)
        case_name = '{0}_{1}'.format(stid.lower(), date_short)
        cmd = [sys.executable, case_gen,
               '--location', '{0:.6f}'.format(lon), '{0:.6f}'.format(lat),
               '--date', date_full,
               '--in_dir', args.in_dir,
               '--grid_dir', args.grid_dir,
               '--forcing_dir', args.forcing_dir,
               '--case_name', case_name] + extra

        print('[{0}/{1}] {2}  (lon={3:.6f}, lat={4:.6f}){5}'.format(
            n, len(sites), case_name, lon, lat, note))
        print('  ' + ' '.join(cmd))

        if args.dry_run:
            continue

        rc = subprocess.call(cmd, cwd=work_dir)
        if rc != 0:
            print('  *** FAILED (exit {0})'.format(rc))
            failed.append(case_name)
        print('')

    if failed:
        print('{0} of {1} case(s) failed: {2}'.format(
            len(failed), len(sites), ', '.join(failed)))
        return 1

    if not args.dry_run:
        print('All {0} case(s) generated.'.format(len(sites)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
