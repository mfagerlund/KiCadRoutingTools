#!/usr/bin/env python3
"""KICAD_RELAX_CORNER and KICAD_RELAX_VIA_GRID: keep-outs at the rule
distance instead of rounded out to the grid.

By default a pad keep-out adds half a cell to its margin (corner buffer),
and a via's track keep-out is rounded up to whole cells plus a quarter-cell
diagonal margin. Both block cells whose centre already clears the rule.

Checks (each in a fresh process, the knobs are read at import):
  1. Both knobs are opt-in (off by default).
  2. Pad, 0.1 mm grid, 0.2 mm margin: with KICAD_RELAX_CORNER a cell
     0.224 mm off the pad corner is free (blocked without the knob); cells
     0.1 mm off the edge and 0.141 mm off the corner stay blocked.
  3. Via track keep-out of 0.23 mm on a 0.1 mm grid: with
     KICAD_RELAX_VIA_GRID the radius is 2.3 cells (3 without, plus the
     quarter-cell diagonal margin), and the stamped disc leaves cells 0.3 mm
     and 0.283 mm off the centre free (blocked without) while a cell 0.2 mm
     off stays blocked.

    python3 tests/test_NNNN_relax_grid.py
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []

PROBE = r'''
import json, sys, os
sys.path.insert(0, os.path.join(sys.argv[1], 'py_router'))
import env_knobs
from routing_utils import iter_pad_blocked_cells, via_track_cells
from routing_config import GridCoord
from obstacle_map import _add_via_obstacle

class _Sink:
    def __init__(self):
        self.cells = set()

# Pad 1.0 x 1.0 mm at cell (0,0): its right edge is 0.5 mm = 5 cells out.
cells = set(iter_pad_blocked_cells(0, 0, 0.5, 0.5, 0.2, 0.1))
coord = GridCoord(0.1)
r = via_track_cells(coord, 0.23)

class _Via:
    x = 0.0; y = 0.0; size = 0.3; layers = ['F.Cu']; net_id = 1
blocked = [set()]
class _Obs:
    def add_blocked_cells_batch(self, *a, **k):
        pass
    def __getattr__(self, name):
        return lambda *a, **k: None
_add_via_obstacle(_Obs(), _Via(), coord, 1, [r], 1.0, diagonal_margin=0.25,
                  blocked_cells=blocked, routing_layers=['F.Cu'])
print(json.dumps({
    'corner': bool(env_knobs.RELAX_CORNER), 'via': bool(env_knobs.RELAX_VIA_GRID),
    'width': bool(env_knobs.RELAX_WIDTH),
    'pad_6': (6, 0) in cells,
    'pad_76': (7, 6) in cells,
    'pad_diag': (6, 6) in cells,
    'radius': r,
    'via_2': (2, 0) in blocked[0], 'via_diag': (2, 2) in blocked[0],
    'via_3': (3, 0) in blocked[0], 'stamped': len(blocked[0]),
}))
'''


def check(name, ok, detail=''):
    print(('PASS' if ok else 'FAIL') + f': {name}' + (f' -- {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def probe(**knobs):
    env = {k: v for k, v in os.environ.items() if not k.startswith('KICAD_RELAX_')}
    env.update({k: '1' for k in knobs})
    r = subprocess.run([sys.executable, '-X', 'utf8', '-c', PROBE, ROOT],
                       capture_output=True, text=True, env=env, timeout=120)
    if r.returncode != 0:
        return {'error': r.stderr[-600:]}
    return json.loads(r.stdout.strip().splitlines()[-1])


off = probe()
print("1. opt-in")
check("probe runs", 'error' not in off, off.get('error', ''))
check("all relax knobs off by default",
      not (off.get('corner') or off.get('via') or off.get('width')))

print("2. pad corner buffer")
on = probe(KICAD_RELAX_CORNER=1)
check("probe runs", 'error' not in on, on.get('error', ''))
# Edge at 5 cells; margin 0.2 mm reaches cell 7, the default buffer cell 7.5.
check("default: cell 0.1 mm off the edge is blocked", off.get('pad_6') is True, str(off))
check("default: cell 0.224 mm off the corner is blocked (half-cell buffer)",
      off.get('pad_76') is True, str(off))
check("knob: cell 0.224 mm off the corner is free", on.get('pad_76') is False, str(on))
check("default: diagonal cell (0.141 mm off the corner + buffer) blocked",
      off.get('pad_diag') is True, str(off))
check("knob: cell 0.1 mm off the edge stays blocked", on.get('pad_6') is True, str(on))
check("knob: diagonal cell 0.141 mm off the corner still blocked (< 0.2 mm)",
      on.get('pad_diag') is True, str(on))

print("3. via grid")
on = probe(KICAD_RELAX_VIA_GRID=1)
check("probe runs", 'error' not in on, on.get('error', ''))
check("default radius rounds up to 3 cells", off.get('radius') == 3, str(off.get('radius')))
check("knob radius is the exact 2.3 cells", abs(float(on.get('radius', 0)) - 2.3) < 1e-9,
      str(on.get('radius')))
check("default: cell 0.3 mm off is blocked", off.get('via_3') is True, str(off))
check("knob: cell 0.3 mm off is free", on.get('via_3') is False, str(on))
check("knob: cell 0.2 mm off stays blocked", on.get('via_2') is True, str(on))
check("knob: diagonal cell 0.283 mm off is free", on.get('via_diag') is False, str(on))
check("knob stamps fewer cells", 0 < on.get('stamped', 0) < off.get('stamped', 0),
      f"{on.get('stamped')} vs {off.get('stamped')}")

print()
if fails:
    print(f"FAILED {len(fails)}: " + '; '.join(fails))
    sys.exit(1)
print("ALL PASS")
