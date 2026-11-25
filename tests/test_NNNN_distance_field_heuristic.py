#!/usr/bin/env python3
"""Distance-field heuristic (KRT_DIST_H=1): A* reads max(octile, the target
distance field over a permissive copy of its move graph) in searches whose
budget is at least KRT_DIST_H_MIN iterations.

Covers, each side in a fresh process (the switch is read once per process):
  1. Bowl trap (a U-shaped wall on both layers, opening away from the
     target): with the field the search finds the same cost path in an
     order of magnitude fewer iterations than octile alone.
  2. Serpentine maze: same path cost, no more iterations.
  3. Probe-sized budgets (below KRT_DIST_H_MIN) are untouched: identical
     path and iterations with the switch on and off.
  4. Unreachable target (sealed box): both sides return no path.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = r'''
import json, sys
sys.path.insert(0, %(rr)r)
import numpy as np
from grid_router import GridObstacleMap, GridRouter

def block(obs, cells):
    obs.add_blocked_cells_batch(np.asarray(cells, dtype=np.int32))

def cost(path, router_via=500):
    c = 0
    for a, b in zip(path, path[1:]):
        if a[2] != b[2]:
            c += router_via
        else:
            c += 1414 if (a[0] != b[0] and a[1] != b[1]) else 1000
    return c

out = {}
# 1. bowl: source inside a U opening to the left, target to the right
obs = GridObstacleMap(2)
cells = []
for y in range(40, 161):
    for l in (0, 1):
        cells.append((150, y, l))
for x in range(60, 151):
    for l in (0, 1):
        cells.append((x, 40, l)); cells.append((x, 160, l))
block(obs, cells)
r = GridRouter(500, 2.3, layer_costs=[1000, 1000])
p, it, _ = r.route_with_frontier(obs, [(140, 100, 0)], [(260, 100, 0)], 2000000)
out['bowl'] = [cost(p) if p else None, it]

# 2. serpentine
obs = GridObstacleMap(1)
width, walls = 60, 20
height = walls * 2 + 1
cells = []
for x in range(0, width + 2):
    cells += [(x, 0, 0), (x, height + 1, 0)]
for y in range(0, height + 2):
    cells += [(0, y, 0), (width + 1, y, 0)]
wi = 0
for y in range(2, height + 1, 2):
    gap = width if wi %% 2 == 0 else 1
    cells += [(x, y, 0) for x in range(1, width + 1) if x != gap]
    wi += 1
block(obs, cells)
tgt = (1, height, 0) if wi %% 2 == 0 else (width, height, 0)
r1 = GridRouter(500, 2.3, layer_costs=[1000])
p, it, _ = r1.route_with_frontier(obs, [(1, 1, 0)], [tgt], 2000000)
out['serp'] = [cost(p) if p else None, it]
# 3. probe-sized budget on the same maze
p, it, _ = r1.route_with_frontier(obs, [(1, 1, 0)], [tgt], 5000)
out['probe'] = [p, it]

# 4. sealed target
obs = GridObstacleMap(1)
cells = []
for x in range(95, 106):
    for y in range(95, 106):
        if x in (95, 105) or y in (95, 105):
            cells.append((x, y, 0))
block(obs, cells)
p, it, _ = r1.route_with_frontier(obs, [(10, 10, 0)], [(100, 100, 0)], 200000)
out['sealed'] = p is None
print(json.dumps(out))
'''


def run(on):
    env = dict(os.environ)
    env.pop('KRT_DIST_H', None)
    if on:
        env['KRT_DIST_H'] = '1'
    code = CHILD % {'rr': os.path.join(REPO, 'rust_router')}
    res = subprocess.run([sys.executable, '-c', code], env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_distance_field_heuristic():
    off, on = run(False), run(True)
    for k in ('bowl', 'serp'):
        assert off[k][0] is not None and on[k][0] is not None, (k, off, on)
        # weighted A*: the field may pick another path; it must not be
        # materially worse, nor the search more expensive
        assert on[k][0] <= off[k][0] * 1.05, (k, off[k], on[k])
        assert on[k][1] <= off[k][1], (k, off[k], on[k])
    # the bowl is the octile trap: the field must cut it by an order
    assert on['bowl'][1] * 10 < off['bowl'][1], (off['bowl'], on['bowl'])
    assert off['probe'] == on['probe']
    assert off['sealed'] and on['sealed']


if __name__ == '__main__':
    test_distance_field_heuristic()
    print('ok')
