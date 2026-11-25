#!/usr/bin/env python3
"""`add_stub_proximity_costs_batch` stamps the same stub-proximity map as the
per-cell loop it replaces.

The batch recomputed the disk's falloff (a sqrt per cell) for every stub and
did a lookup plus a second insert lookup per cell. It now builds the disk's
(dx, dy, cost) table once per call in the same scan order, drops cells whose
cost is <= 0 (they can never raise a stored value: every stored value is
> 0), updates hits in place, and stamps a stub repeated at the same grid
point once (a repeat changes nothing under max composition).

Invariants gated here (oracle: the old loop in Python with float32 maths):
  1. Random stubs (duplicates, negative coordinates, overlapping disks),
     radii 0..9 and costs -3..1000 (many zero-cost rims) over a map that
     already holds values from set_stub_proximity: every cell equals the
     reference, and so does the cell count.
  2. Two batches in a row compose by max like before.

Run:
    python3 tests/test_2045_stub_proximity_batch_disk.py
"""

import os
import random
import sys

import numpy as np

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, 'rust_router'))

from grid_router import GridObstacleMap

#: One small Rust map per case; no routing, no subprocesses.
RUN_ALL_FAST_OK = True

F32 = np.float32


def _ref_batch(ref, stubs, radius, max_cost):
    """The old loop: f32 falloff, max-insert above the 0 default."""
    radius_sq = radius * radius
    radius_f = F32(radius)
    for gcx, gcy in stubs:
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                dist_sq = dx * dx + dy * dy
                if dist_sq <= radius_sq:
                    dist = np.sqrt(F32(dist_sq))
                    with np.errstate(invalid='ignore'):
                        proximity = F32(1.0) - (dist / radius_f)
                    cost = _as_i32(proximity * F32(max_cost))
                    key = (gcx + dx, gcy + dy)
                    if cost > ref.get(key, 0):
                        ref[key] = cost


def _ref_set(ref, gx, gy, cost):
    if cost > ref.get((gx, gy), 0):
        ref[(gx, gy)] = cost


def _check(obs, ref, span):
    for gx in range(-span, span + 1):
        for gy in range(-span, span + 1):
            want = ref.get((gx, gy), 0)
            got = obs.get_stub_proximity_cost(gx, gy)
            assert got == want, ((gx, gy), got, want)


def _count(obs):
    return obs.get_stats()[2]   # stub_proximity_count


def _as_i32(x):
    """Rust `f32 as i32`: truncate, NaN -> 0 (radius 0 divides 0 by 0)."""
    return 0 if np.isnan(x) else int(x)


def test_batch_matches_reference():
    rng = random.Random(2045)
    for case in range(300):
        obs = GridObstacleMap(2)
        ref = {}
        for _ in range(rng.randint(0, 20)):
            gx, gy = rng.randint(-12, 12), rng.randint(-12, 12)
            c = rng.randint(-5, 600)
            obs.set_stub_proximity(gx, gy, c)
            _ref_set(ref, gx, gy, c)
        for _ in range(rng.randint(1, 2)):
            pts = [(rng.randint(-10, 10), rng.randint(-10, 10))
                   for _ in range(rng.randint(0, 12))]
            if pts and rng.random() < 0.5:
                pts += rng.sample(pts, min(3, len(pts)))
            radius = rng.randint(0, 9)
            max_cost = rng.choice([-3, 0, 1, 2, 3, 7, 50, 333, 1000])
            obs.add_stub_proximity_costs_batch(pts, radius, max_cost)
            _ref_batch(ref, pts, radius, max_cost)
        _check(obs, ref, 21)


def test_cell_count_matches_reference():
    rng = random.Random(45)
    for case in range(100):
        obs = GridObstacleMap(2)
        ref = {}
        pts = [(rng.randint(-30, 30), rng.randint(-30, 30)) for _ in range(25)]
        radius, max_cost = rng.randint(1, 12), rng.choice([1, 3, 10, 400])
        obs.add_stub_proximity_costs_batch(pts, radius, max_cost)
        _ref_batch(ref, pts, radius, max_cost)
        _check(obs, ref, 44)
        assert _count(obs) == len(ref), (_count(obs), len(ref))


if __name__ == '__main__':
    test_batch_matches_reference()
    test_cell_count_matches_reference()
    print('ok')
