#!/usr/bin/env python
"""Extract a small regional subset of the UFS history files used as SCM forcing.

Why this exists
---------------
The C768/L127 atmf files are chunked one full horizontal level per chunk
(1 x 1 x 1536 x 3072, deflated). A chunk is ~18.9 MB, which is larger than the
netCDF chunk cache, so reading a single 127-level column inflates ~2.4 GB to
return 127 floats -- and nothing is ever reused. UFS_case_gen.py needs only a
5x5 halo per station, so >99.99% of that decompression is wasted, and it is
repeated for every station.

This script pays the decompression cost once: it reads each (file, variable)
pair exactly once, in parallel across processes, and writes a small regional
subset that still looks like a UFS history directory. Point UFS_case_gen.py's
--forcing_dir at the output and it runs unchanged, reading files small enough
to be effectively free.

The per-(file, variable) reads are independent, so this parallelises even for a
single station -- 9 files x 6 3-D variables = 54 tasks either way.

Note on backends: the work is embarrassingly parallel with coarse tasks and no
shared graph, so the stdlib process pool is used and needs no extra dependency.
Processes, not threads: netCDF4/HDF5 serialises reads under a global lock, so
threads would give near-zero speedup.

Examples
--------
  # every station in the CSV, 18 workers
  source casegen.env
  ./extract_forcing_subset.py -o $CASEGEN_SUBSET_DIR

  # a single station
  ./extract_forcing_subset.py -s FLUX_BELL -o /path/to/out --workers 12
"""

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from netCDF4 import Dataset

METADATA_CSV = os.environ.get('CASEGEN_SITES_CSV', '')
FORCING_DIR = os.environ.get('CASEGEN_FORCING_DIR', '')

COL_STID, COL_LAT, COL_LON = 'stid', 'lat [degrees]', 'lon [degrees]'

# Fields UFS_case_gen.py reads from the atmf files:
#   get_UFS_forcing_data_advective_tendency() -- ugrd vgrd dzdt tmp spfh delz pressfc
#   get_IC_data_from_UFS_history()            -- adds clwmr icmr o3mr (forcing_method 2
#                                                takes the initial state from history)
#   geostrophic path                          -- hgtsfc (2-D, nearly free to carry)
# lon/lat/pfull/phalf/time and the 1-D coordinates are picked up automatically.
ATM_VARS = ['ugrd', 'vgrd', 'dzdt', 'tmp', 'spfh', 'delz', 'pressfc',
            'clwmr', 'icmr', 'o3mr', 'hgtsfc']

# Dimensions that get subset; everything else is copied whole.
YDIM, XDIM = 'grid_yt', 'grid_xt'

EARTH_RADIUS = 6371000.0


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('-f', '--forcing_dir', default=FORCING_DIR,
                   help='source UFS history directory (default: %(default)s)')
    p.add_argument('-o', '--out_dir', required=True,
                   help='destination directory for the subset files')
    p.add_argument('--csv', default=METADATA_CSV, help='station metadata CSV')
    p.add_argument('-s', '--sites', nargs='+', metavar='STID',
                   help='only cover these stations (default: all in the CSV)')
    p.add_argument('-m', '--margin', type=int, default=8,
                   help='grid points of padding around the station bounding box '
                        '(default: %(default)s). Must exceed '
                        'n_forcing_halo_points (2); increase it substantially '
                        'if you intend to run with --geostrophic, which expands '
                        'the stencil at runtime.')
    p.add_argument('-w', '--workers', type=int, default=18,
                   help='parallel worker processes (default: %(default)s)')
    p.add_argument('-b', '--backend', choices=['processes', 'serial'],
                   default='processes', help='execution backend')
    p.add_argument('--atm_vars', nargs='+', default=ATM_VARS,
                   help='atmf variables to carry over')
    p.add_argument('--dry-run', action='store_true',
                   help='report the box and task list, then stop')
    return p.parse_args()


def read_sites(csv_file, wanted):
    with open(csv_file) as fh:
        rows = list(csv.DictReader(fh))
    sites = []
    for row in rows:
        stid = row[COL_STID].strip()
        if wanted and stid not in wanted:
            continue
        sites.append((stid, float(row[COL_LON]) % 360.0, float(row[COL_LAT])))
    if wanted:
        missing = sorted(set(wanted) - {s[0] for s in sites})
        if missing:
            raise ValueError('Not in CSV: ' + ', '.join(missing))
    return sites


def haversine_grid(lat_grid, lon_grid, lat0, lon0):
    """Vectorised haversine distance (m) from one point to every grid cell.

    Mirrors haversine_distance() in UFS_case_gen.py so the nearest-neighbour
    choice is identical, but without its per-cell Python loop.
    """
    la1, lo1 = np.radians(lat_grid), np.radians(lon_grid)
    la2, lo2 = np.radians(lat0), np.radians(lon0)
    dlat, dlon = la2 - la1, lo2 - lo1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(la1) * np.cos(la2) * np.sin(dlon / 2.0) ** 2
    return 2.0 * EARTH_RADIUS * np.arcsin(np.sqrt(a))


def locate_box(forcing_dir, sites, margin):
    """Find the index bounding box on the history grid covering all stations."""
    src = os.path.join(forcing_dir, 'atmf000.nc')
    nc = Dataset(src)
    nc.set_always_mask(False)
    lon = np.asarray(nc['lon'])
    lat = np.asarray(nc['lat'])
    ny, nx = lon.shape
    nc.close()

    located = []
    for stid, slon, slat in sites:
        d = haversine_grid(lat, lon, slat, slon)
        iy, ix = np.unravel_index(d.argmin(), d.shape)
        located.append((stid, iy, ix, d[iy, ix] / 1000.0))

    iys = [p[1] for p in located]
    ixs = [p[2] for p in located]
    y0, y1 = min(iys) - margin, max(iys) + margin + 1
    x0, x1 = min(ixs) - margin, max(ixs) + margin + 1

    if y0 < 0 or y1 > ny:
        raise ValueError('Station box runs off the grid in latitude; the SCM '
                         'halo logic cannot handle points that close to a pole.')
    if x0 < 0 or x1 > nx:
        raise ValueError(
            'Station box wraps the prime meridian (x range {0}:{1} of {2}). '
            'This script only emits contiguous boxes -- a wrapped subset would '
            'silently break the cyclic halo logic in UFS_case_gen.py. Split the '
            'stations into two runs.'.format(x0, x1, nx))

    return located, (y0, y1, x0, x1), (ny, nx)


def subset_slices(dims, box):
    """Build an index tuple that subsets only the horizontal dimensions."""
    y0, y1, x0, x1 = box
    out = []
    for d in dims:
        if d == YDIM:
            out.append(slice(y0, y1))
        elif d == XDIM:
            out.append(slice(x0, x1))
        else:
            out.append(slice(None))
    return tuple(out)


def read_one(task):
    """Worker: read one variable's regional subset. Runs in its own process."""
    fname, src, varname, box = task
    t0 = time.time()
    nc = Dataset(src)
    nc.set_always_mask(False)
    var = nc[varname]
    data = np.asarray(var[subset_slices(var.dimensions, box)])
    nc.close()
    return fname, varname, data, time.time() - t0


def horizontal_vars(src, wanted=None):
    """Variables carrying horizontal dims, plus the coordinate/ancillary vars."""
    nc = Dataset(src)
    hor, other = [], []
    for name, var in nc.variables.items():
        if YDIM in var.dimensions or XDIM in var.dimensions:
            if wanted is None or name in wanted or var.ndim <= 2:
                hor.append(name)
        else:
            other.append(name)
    nc.close()
    return hor, other


def write_subset(src, dst, box, arrays, other_vars):
    """Write the subset file, preserving structure, attrs and global attrs."""
    y0, y1, x0, x1 = box
    nin = Dataset(src)
    nin.set_always_mask(False)
    out = Dataset(dst, 'w', format='NETCDF4')

    out.setncatts({k: nin.getncattr(k) for k in nin.ncattrs()})

    for name, dim in nin.dimensions.items():
        if name == YDIM:
            out.createDimension(name, y1 - y0)
        elif name == XDIM:
            out.createDimension(name, x1 - x0)
        elif dim.isunlimited():
            out.createDimension(name, None)
        else:
            out.createDimension(name, len(dim))

    def copy_var(name, values):
        src_var = nin[name]
        fill = src_var.getncattr('_FillValue') if '_FillValue' in src_var.ncattrs() else None
        new = out.createVariable(name, src_var.dtype, src_var.dimensions,
                                 fill_value=fill)
        for a in src_var.ncattrs():
            if a != '_FillValue':
                new.setncattr(a, src_var.getncattr(a))
        new[...] = values

    for name in other_vars:
        copy_var(name, np.asarray(nin[name][...]))
    for name, values in arrays.items():
        copy_var(name, values)

    out.close()
    nin.close()


def run_tasks(tasks, backend, workers):
    """Execute read tasks, returning [(varname, data, seconds), ...]."""
    if backend == 'serial':
        return [read_one(t) for t in tasks]

    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(read_one, tasks))


def main():
    args = parse_args()
    sites = read_sites(args.csv, set(args.sites) if args.sites else None)
    if not sites:
        print('No sites selected.')
        return 1
    if args.margin < 3:
        print('WARNING: margin < 3 leaves no room around the 5x5 forcing halo.')

    print('Locating stations on the history grid...')
    t0 = time.time()
    located, box, (ny, nx) = locate_box(args.forcing_dir, sites, args.margin)
    y0, y1, x0, x1 = box
    print('  took %.1f s' % (time.time() - t0))
    for stid, iy, ix, dist_km in located:
        print('    %-10s -> grid_yt=%4d grid_xt=%4d  (%.2f km away)'
              % (stid, iy, ix, dist_km))
    print('  box: grid_yt %d:%d, grid_xt %d:%d  ->  %d x %d  (from %d x %d)'
          % (y0, y1, x0, x1, y1 - y0, x1 - x0, ny, nx))

    files = sorted(f for f in os.listdir(args.forcing_dir)
                   if f.startswith(('atmf', 'sfcf')) and f.endswith('.nc'))
    if not files:
        raise SystemExit('No atmf*/sfcf* files in ' + args.forcing_dir)

    plan = []
    for fname in files:
        src = os.path.join(args.forcing_dir, fname)
        wanted = set(args.atm_vars) if fname.startswith('atmf') else None
        hor, other = horizontal_vars(src, wanted)
        plan.append((fname, src, hor, other))

    n_tasks = sum(len(h) for _, _, h, _ in plan)
    print('\n%d files, %d read tasks, backend=%s, workers=%d'
          % (len(files), n_tasks, args.backend, args.workers))

    if args.dry_run:
        for fname, _, hor, other in plan:
            print('  %-12s subset: %s' % (fname, ', '.join(hor)))
        return 0

    os.makedirs(args.out_dir, exist_ok=True)

    #Flatten every (file, variable) pair into one task pool so reads from
    #different files overlap too. Each task decompresses an independent set of
    #chunks, so wall time tends toward (slowest single task) rather than the sum.
    #The gathered subsets are tiny, so holding them all before writing is cheap.
    t_start = time.time()
    tasks = [(fname, src, v, box) for fname, src, hor, _ in plan for v in hor]
    results = run_tasks(tasks, args.backend, args.workers)
    t_read = time.time() - t_start

    gathered = {}
    for fname, varname, data, _ in results:
        gathered.setdefault(fname, {})[varname] = data
    slowest = max((s for _, _, _, s in results), default=0.0)
    print('  read %d tasks in %.1f s (slowest single task %.1f s)'
          % (len(tasks), t_read, slowest))

    for fname, src, hor, other in plan:
        dst = os.path.join(args.out_dir, fname)
        write_subset(src, dst, box, gathered[fname], other)
        print('  %-12s %3d vars  wrote %6.1f MB'
              % (fname, len(hor), os.path.getsize(dst) / 1.0e6))
        sys.stdout.flush()

    print('\nTotal: %.1f s' % (time.time() - t_start))
    print('Now run the case generator against the subset:')
    print('  gen_scm_cases.py --forcing_dir %s' % args.out_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
