#!/usr/bin/env python
"""Resolve SCM target locations for the flux stations against the model masks.

UFS_case_gen.py resolves TWO independent nearest points: one on the C768 native
grid (supplying surface + orography) and one on the quilted history grid
(supplying state + forcing). Near a coastline those land on opposite sides, and
check_IC_hist_surface_compatibility() aborts the case.

Worse, that built-in check only tests *agreement*, not *land*: a station whose
two points are BOTH water passes cleanly and yields a lake/ocean column at a
land flux tower.

This script classifies every station and, where needed, finds the closest target
where both grids agree on land. Candidates are always the CENTRE of a history
grid cell, which guarantees that cell wins the history nearest-neighbour search,
so only the IC grid has to be verified.

It deliberately does not patch UFS_case_gen.py -- the built-in check stays armed
as an independent verifier of whatever this script recommends.

Examples
--------
  # classify only
  ./check_land_mask.py --map

  # write the manifest consumed by gen_scm_cases.py --locations
  source casegen.env
  ./check_land_mask.py --policy require-land --write_csv $CASEGEN_LOCATIONS_CSV
"""

import argparse
import csv
import os
import sys

import numpy as np
from netCDF4 import Dataset

# UFS_case_gen.py lives inside the user's own CCPP-SCM checkout; it is imported
# for its grid-search helpers rather than re-implementing them here.
SCM_ROOT = os.environ.get('CASEGEN_SCM_ROOT')
_scm_root_cli = None
for _i, _a in enumerate(sys.argv):
    if _a in ('-r', '--scm_root') and _i + 1 < len(sys.argv):
        _scm_root_cli = sys.argv[_i + 1]
    elif _a.startswith('--scm_root='):
        _scm_root_cli = _a.split('=', 1)[1]
if _scm_root_cli:
    SCM_ROOT = _scm_root_cli
if not SCM_ROOT:
    sys.exit('ERROR: set CASEGEN_SCM_ROOT (source casegen.env) or pass '
             '--scm_root /path/to/ccpp-scm')
_CASE_GEN_DIR = os.path.join(SCM_ROOT, 'scm', 'etc', 'scripts')
if not os.path.isfile(os.path.join(_CASE_GEN_DIR, 'UFS_case_gen.py')):
    sys.exit(f'ERROR: no UFS_case_gen.py under {_CASE_GEN_DIR}')
sys.path.insert(0, _CASE_GEN_DIR)
import UFS_case_gen as U

METADATA_CSV = os.environ.get('CASEGEN_SITES_CSV', '')
IN_DIR = os.environ.get('CASEGEN_IC_DIR', '')
FORCING_DIR = os.environ.get('CASEGEN_SUBSET_DIR',
                             os.environ.get('CASEGEN_FORCING_DIR', ''))

COL_STID, COL_LAT, COL_LON = 'stid', 'lat [degrees]', 'lon [degrees]'

# slmsk in the IC sfc_data files: 0=water, 1=land, 2=ice.
LAND = 1.0


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--csv', default=METADATA_CSV, help='station metadata CSV')
    p.add_argument('-i', '--in_dir', default=IN_DIR)
    p.add_argument('-g', '--grid_dir', default=IN_DIR)
    p.add_argument('-f', '--forcing_dir', default=FORCING_DIR)
    p.add_argument('-s', '--sites', nargs='+', metavar='STID')
    p.add_argument('--policy', default='report',
                   choices=['report', 'on-mismatch', 'require-land'],
                   help='report: classify only. on-mismatch: relocate only when '
                        'the two grids disagree (matches the built-in check). '
                        'require-land: also relocate stations that are water on '
                        'both grids -- recommended for a land flux-tower study, '
                        'since those pass the built-in check silently.')
    p.add_argument('--write_csv', metavar='PATH',
                   help='write the resolved-location manifest here')
    p.add_argument('--map', action='store_true',
                   help='print a local history-grid land mask map for problems')
    p.add_argument('-r', '--scm_root', default=SCM_ROOT,
                   help='CCPP-SCM checkout holding scm/etc/scripts/UFS_case_gen.py')
    p.add_argument('--halo', type=int, default=6)
    p.add_argument('--max_candidates', type=int, default=8)
    return p.parse_args()


def haversine_vec(lat1, lon1, lat2, lon2):
    """Vectorised form of haversine_distance() in UFS_case_gen.py (metres)."""
    p = np.pi / 180.0
    hav = (0.5 - np.cos((lat2 - lat1) * p) / 2.0
           + np.cos(lat1 * p) * np.cos(lat2 * p)
           * (1.0 - np.cos((lon2 - lon1) * p)) / 2.0)
    return 2.0 * U.earth_radius * np.arcsin(np.sqrt(hav))


def read_sites(csv_file, wanted):
    with open(csv_file) as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for row in rows:
        stid = row[COL_STID].strip()
        if wanted and stid not in wanted:
            continue
        out.append((stid, float(row[COL_LON]) % 360.0, float(row[COL_LAT])))
    return out


def classify(loc, args, land_all):
    """Return (ic_mask, hist_mask, ic_pt, hist_pt) for a target location."""
    (ic_i, ic_j, tile, _, _, _, _) = U.find_loc_indices_UFS_IC(
        loc, args.grid_dir, False, None, None)
    (h_i, h_j, _, _, _, _, _, _, _) = U.find_loc_indices_UFS_history(
        loc, args.forcing_dir, False)
    surface = U.get_UFS_surface_data(args.in_dir, tile, ic_i, ic_j, False, False)
    return surface['slmsk'], land_all[h_i, h_j], (tile, ic_i, ic_j), (h_i, h_j)


def status_of(ic_mask, hist_mask):
    if ic_mask != hist_mask:
        return 'mismatch'
    if ic_mask != LAND:
        return 'water-both'
    return 'land-both'


def needs_move(status, policy):
    if policy == 'on-mismatch':
        return status == 'mismatch'
    if policy == 'require-land':
        return status != 'land-both'
    return False


def find_land_target(loc, args, land_all, hlon, hlat):
    """Closest history-grid land-cell centre where the IC grid is also land."""
    d = np.full(hlat.shape, np.inf)
    land = (land_all == LAND)
    d[land] = haversine_vec(hlat[land], hlon[land], loc[1], loc[0])
    for flat in np.argsort(d, axis=None)[:args.max_candidates]:
        ci, cj = np.unravel_index(flat, d.shape)
        clon, clat = float(hlon[ci, cj]), float(hlat[ci, cj])
        (a_i, a_j, a_tile, _, _, _, _) = U.find_loc_indices_UFS_IC(
            (clon, clat), args.grid_dir, False, None, None)
        surf = U.get_UFS_surface_data(args.in_dir, a_tile, a_i, a_j, False, False)
        if surf['slmsk'] == LAND:
            return clon, clat, float(d[ci, cj]) / 1000.0
    return None, None, None


def print_map(stid, land_all, h_i, h_j, halo):
    print('\n%s: history-grid land mask near (%d,%d)  '
          '(#=land, .=water; X = selected point; row index increases southward)'
          % (stid, h_i, h_j))
    for di in range(-halo, halo + 1):
        row = []
        for dj in range(-halo, halo + 1):
            v = land_all[h_i + di, h_j + dj]
            row.append('X' if (di == 0 and dj == 0) else ('#' if v == LAND else '.'))
        print('     ' + ' '.join(row))


def main():
    args = parse_args()
    sites = read_sites(args.csv, set(args.sites) if args.sites else None)

    hist_file = os.path.join(args.forcing_dir, 'sfcf000.nc')
    nc = Dataset(hist_file)
    nc.set_always_mask(False)
    land_all = np.asarray(nc['land'][0, :, :])
    hlon = np.asarray(nc['lon'][:, :])
    hlat = np.asarray(nc['lat'][:, :])
    nc.close()

    print('policy: %s\n' % args.policy)
    print('%-11s %8s %8s  %-12s %s'
          % ('station', 'IC mask', 'hist', 'status', 'action'))
    print('-' * 78)

    records, problems = [], []
    for stid, lon, lat in sites:
        loc = (lon, lat)
        ic_mask, hist_mask, ic_pt, hist_pt = classify(loc, args, land_all)
        status = status_of(ic_mask, hist_mask)

        scm_lon, scm_lat, moved = lon, lat, 0.0
        if needs_move(status, args.policy):
            clon, clat, dist_km = find_land_target(loc, args, land_all, hlon, hlat)
            if clon is None:
                action = 'NO LAND TARGET FOUND'
            else:
                scm_lon, scm_lat, moved = clon, clat, dist_km
                action = 'moved %.1f km' % dist_km
        elif status == 'land-both':
            action = '-'
        else:
            action = 'left as-is (policy)'

        if status != 'land-both':
            problems.append((stid, hist_pt))

        print('%-11s %8.1f %8.1f  %-12s %s'
              % (stid, ic_mask, hist_mask, status, action))
        records.append({
            'stid': stid, 'lon': '%.6f' % lon, 'lat': '%.6f' % lat,
            'scm_lon': '%.6f' % scm_lon, 'scm_lat': '%.6f' % scm_lat,
            'displacement_km': '%.2f' % moved,
            'ic_mask': '%.1f' % ic_mask, 'hist_mask': '%.1f' % hist_mask,
            'status': status, 'action': action})

    if args.map:
        for stid, (h_i, h_j) in problems:
            print_map(stid, land_all, h_i, h_j, args.halo)

    if args.write_csv:
        fields = ['stid', 'lon', 'lat', 'scm_lon', 'scm_lat',
                  'displacement_km', 'ic_mask', 'hist_mask', 'status', 'action']
        with open(args.write_csv, 'w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(records)
        print('\nWrote manifest: %s' % args.write_csv)
        print('Use it with: gen_scm_cases.py --locations %s'
              % args.write_csv)

    n_moved = sum(1 for r in records if float(r['displacement_km']) > 0)
    print('\n%d station(s), %d relocated, %d still not land-on-both.'
          % (len(records), n_moved,
             sum(1 for r in records
                 if r['status'] != 'land-both' and float(r['displacement_km']) == 0)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
