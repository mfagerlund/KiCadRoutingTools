#!/usr/bin/env python3
"""KICAD_RELAX_CORNER and KICAD_RELAX_VIA_GRID keep every grid STEP clear.

The router walks 8-connected grid steps and only tests cells, so a keep-out
must block enough cells that no step between two free cells passes inside
the margin. A keep-out at the bare margin round a pad's corner lets a 45-degree step clip the corner: both ends clear, its middle
does not (a 0.127 mm neck-down leaving a 0.4 mm pitch QFN pad's tip at 45
degrees passed its neighbour's corner 0.169 mm off for a 0.175 mm rule).
The via keep-out already rounds to whole cells and is held as a guard.

Checks, each in a fresh process (the knobs are read at import), pads at
several sizes, corner radii, margins and sub-cell offsets, vias at several
radii and offsets:
  1. With each knob on, every step between two free cells keeps the margin
     to the true pad / via shape along its whole length.
  2. The knobs still free cells the default blocks (the relaxation stays).
  3. Off, the default keep-outs keep every step clear as well.

    python3 tests/test_NNNN_relax_step_clearance.py
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []

PROBE = r'''
import json, math, sys, os
sys.path.insert(0, os.path.join(sys.argv[1], 'py_router'))
from routing_utils import iter_pad_blocked_cells, dist_sq_to_rounded_rect, via_track_cells
from routing_config import GridCoord
from obstacle_map import _add_via_obstacle

G = 0.1
STEPS = [(1, 0), (0, 1), (1, 1), (1, -1)]

def seg_min(fn, ax, ay, bx, by, n=64):
    return min(fn(ax + (bx - ax) * i / n, ay + (by - ay) * i / n) for i in range(n + 1))

def worst_step(blocked, fn, margin, cx, cy, reach):
    """smallest (min distance along a free-to-free step) - margin; cells in grid units round (cx, cy)."""
    worst = 1e9
    for gx in range(cx - reach, cx + reach + 1):
        for gy in range(cy - reach, cy + reach + 1):
            if (gx, gy) in blocked:
                continue
            for dx, dy in STEPS:
                b = (gx + dx, gy + dy)
                if b in blocked:
                    continue
                d = seg_min(fn, gx * G, gy * G, b[0] * G, b[1] * G)
                worst = min(worst, d - margin)
    return worst

out = {'pad_worst': 1e9, 'pad_free': 0, 'via_worst': 1e9, 'via_free': 0}
# pads: (half w, half h, corner radius); centre at cell (50, 50) plus a sub-cell offset
for hw, hh, cr in [(0.4375, 0.1, 0.03876), (0.4375, 0.1, 0.0), (0.3, 0.3, 0.075), (0.25, 0.125, 0.0)]:
    for margin in (0.2385, 0.2, 0.1635, 0.29):
        for ox, oy in [(0.0, 0.0), (0.03, 0.0), (0.05, 0.05), (0.02, 0.045)]:
            px, py = 50 * G + ox, 50 * G + oy
            blocked = set(iter_pad_blocked_cells(50, 50, hw, hh, margin, G, cr, None, ox, oy))
            fn = lambda x, y: math.sqrt(max(dist_sq_to_rounded_rect(x - px, y - py, hw, hh, cr), 0.0))
            reach = int((max(hw, hh) + margin) / G) + 3
            out['pad_worst'] = min(out['pad_worst'], worst_step(blocked, fn, margin, 50, 50, reach))
            out['pad_free'] += sum(1 for gx in range(50 - reach, 51 + reach) for gy in range(50 - reach, 51 + reach)
                                   if (gx, gy) not in blocked)

class _Obs:
    def add_blocked_cells_batch(self, *a, **k):
        pass
    def __getattr__(self, name):
        return lambda *a, **k: None

class _Via:
    def __init__(self, x, y):
        self.x, self.y, self.size, self.layers, self.net_id = x, y, 0.3, ['F.Cu'], 1

coord = GridCoord(G)
for keep in (0.23, 0.3375, 0.4155):
    for ox, oy in [(0.0, 0.0), (0.04, 0.0), (0.05, 0.05)]:
        vx, vy = 50 * G + ox, 50 * G + oy
        blocked = [set()]
        exp = via_track_cells(coord, keep)
        _add_via_obstacle(_Obs(), _Via(vx, vy), coord, 1, [exp], 1.0, diagonal_margin=0.25,
                          blocked_cells=blocked, routing_layers=['F.Cu'])
        cells = {tuple(c) for c in blocked[0]}
        fn = lambda x, y: math.hypot(x - vx, y - vy)
        reach = int(keep / G) + 4
        out['via_worst'] = min(out['via_worst'], worst_step(cells, fn, keep, 50, 50, reach))
        out['via_free'] += sum(1 for gx in range(50 - reach, 51 + reach) for gy in range(50 - reach, 51 + reach)
                               if (gx, gy) not in cells)
print('PROBE:' + json.dumps(out))
'''


def probe(**env):
    e = {k: v for k, v in os.environ.items() if not k.startswith('KICAD_RELAX')}
    e.update(env)
    r = subprocess.run([sys.executable, '-c', PROBE, ROOT], capture_output=True, text=True, env=e)
    line = [l for l in r.stdout.splitlines() if l.startswith('PROBE:')]
    if not line:
        print(r.stdout[-2000:], r.stderr[-2000:])
        return None
    return json.loads(line[-1][6:])


def check(cond, msg, detail=''):
    print(('PASS: ' if cond else 'FAIL: ') + msg + (f' -- {detail}' if detail != '' else ''))
    if not cond:
        fails.append(msg)


base = probe()
on = probe(KICAD_RELAX_CORNER='1', KICAD_RELAX_VIA_GRID='1')
check(base is not None and on is not None, 'probes ran')
if base is not None and on is not None:
    tol = 1e-9
    check(on['pad_worst'] >= -tol, 'relaxed pad keep-out: every free-to-free step keeps the margin',
          f"worst {on['pad_worst']:.5f} mm")
    check(on['via_worst'] >= -tol, 'relaxed via keep-out: every free-to-free step keeps the margin',
          f"worst {on['via_worst']:.5f} mm")
    check(base['pad_worst'] >= -tol and base['via_worst'] >= -tol, 'default keep-outs keep every step clear',
          f"pad {base['pad_worst']:.5f}, via {base['via_worst']:.5f}")
    check(on['pad_free'] > base['pad_free'], 'relaxed pad keep-out still frees cells',
          f"{on['pad_free']} vs {base['pad_free']}")
    check(on['via_free'] > base['via_free'], 'relaxed via keep-out still frees cells',
          f"{on['via_free']} vs {base['via_free']}")

print('\nALL PASS' if not fails else f'\n{len(fails)} FAIL(S)')
sys.exit(1 if fails else 0)
