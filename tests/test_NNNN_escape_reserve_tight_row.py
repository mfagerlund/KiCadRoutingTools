#!/usr/bin/env python3
"""Escape reservation: no lone dog-bone in a row too tight to pass a via.

In a row whose pitch is below via_size/2 + clearance + track/2, a track
cannot run between a via and the next pin. A dog-bone reserved for one pin
then blocks the straight exit of its neighbours. When such a neighbour (a
different net) found no site of its own, the reservation only takes its
way out: the dog-bone is withdrawn and both pins route unreserved.

Fixture: a column of five pads (- A B C -, the ends unconnected) on F.Cu,
each net ending on a B.Cu pad 9 mm away (every pin needs a via). Fence
tracks of another net box the row in on both layers, with one gap in front
of B: only B has a legal dog-bone site.

  0.50 mm pitch (track cannot pass a via):  B is not reserved.
  0.65 mm pitch (track passes):             B keeps its dog-bone.
  0.50 mm pitch, A and C unconnected:       B keeps its dog-bone.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))
from run_utils import evidence  # noqa: E402

ROW_X = 110.0
ROW_Y = 110.0
PAD_W = 0.8          # along x (the escape direction)
PAD_H = 0.25
FENCE_GAP = 0.15     # pad edge to fence centre line
FENCE_W = 0.1
SLOT = 0.32          # half-height of the gap in front of B


def _smd(num, x, y, sx, sy, net, name, shape, layer):
    return (f'\t\t(pad "{num}" smd {shape}\n\t\t\t(at {x:.4f} {y:.4f})\n'
            f'\t\t\t(size {sx} {sy})\n\t\t\t(layers "{layer}")\n'
            f'\t\t\t(net {net} "{name}")\n\t\t)\n')


def _fp(ref, x, y, pads):
    return (f'\t(footprint "test:{ref}"\n\t\t(layer "F.Cu")\n'
            f'\t\t(at {x:.4f} {y:.4f})\n'
            f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 -5 0)\n'
            f'\t\t\t(layer "F.SilkS")\n'
            f'\t\t\t(effects (font (size 1 1) (thickness 0.15)))\n\t\t)\n'
            + ''.join(pads) + '\t)\n')


def _seg(a, b, layer, net):
    return (f'\t(segment\n\t\t(start {a[0]:.4f} {a[1]:.4f})\n'
            f'\t\t(end {b[0]:.4f} {b[1]:.4f})\n\t\t(width {FENCE_W})\n'
            f'\t\t(layer "{layer}")\n\t\t(net {net})\n\t)\n')


def board_text(pins, pitch):
    """`pins`: net name per row pin, top to bottom ('' = unconnected)."""
    names = sorted(set(pins) - {''}) + ['FENCE']
    ids = {n: 1 + i for i, n in enumerate(names)}
    ids[''] = 0
    nets = ['(net 0 "")'] + [f'(net {ids[n]} "{n}")' for n in names]
    span = (len(pins) - 1) * pitch
    u1, j1 = [], []
    for k, n in enumerate(pins):
        y = -span / 2 + k * pitch
        u1.append(_smd(str(k + 1), 0, y, PAD_W, PAD_H, ids[n], n, 'roundrect', 'F.Cu'))
        jy = (k - (len(pins) - 1) / 2) * 1.5   # coarse: J1 is no fine row
        j1.append(_smd(str(k + 1), 9.0, jy, 0.3, 0.3, ids[n], n, 'circle', 'B.Cu'))
    # Fence box around the row; the right side is open only at B (row centre).
    xl = ROW_X - PAD_W / 2 - FENCE_GAP
    xr = ROW_X + PAD_W / 2 + FENCE_GAP
    yt = ROW_Y - span / 2 - PAD_H / 2 - 0.6
    yb = ROW_Y + span / 2 + PAD_H / 2 + 0.6
    fence = [((xl, yt), (xr, yt)), ((xl, yb), (xr, yb)), ((xl, yt), (xl, yb)),
             ((xr, yt), (xr, ROW_Y - SLOT)), ((xr, ROW_Y + SLOT), (xr, yb))]
    f = ids['FENCE']
    segs = ''.join(_seg(a, b, lay, f) for a, b in fence for lay in ('F.Cu', 'B.Cu'))
    half_h = (len(pins) - 1) * 0.75 + 3.0
    body = _fp('U1', ROW_X, ROW_Y, u1) + _fp('J1', ROW_X, ROW_Y, j1)
    return ('(kicad_pcb\n\t(version 20241229)\n\t(generator "test_NNNN_escape_reserve_tight_row")\n'
            '\t(general\n\t\t(thickness 1.6)\n\t)\n'
            '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(31 "B.Cu" signal)\n'
            '\t\t(44 "Edge.Cuts" user)\n\t)\n\t' + '\n\t'.join(nets) + '\n'
            + body + segs +
            f'\t(gr_rect\n\t\t(start {ROW_X - 4:.4f} {ROW_Y - half_h:.4f})\n'
            f'\t\t(end {ROW_X + 11:.4f} {ROW_Y + half_h:.4f})\n'
            '\t\t(stroke (width 0.1) (type default))\n'
            '\t\t(layer "Edge.Cuts")\n\t)\n)\n')


def _load(d, name, pins, pitch):
    from kicad_parser import parse_kicad_pcb
    p = os.path.join(d, name)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(board_text(pins, pitch))
    return parse_kicad_pcb(evidence(p))


def _config():
    from routing_config import GridRouteConfig
    return GridRouteConfig(track_width=0.2, clearance=0.15, via_size=0.6,
                           via_drill=0.3, grid_step=0.05, layers=['F.Cu', 'B.Cu'])


def _plan(pcb, cfg):
    import escape_reserve as er
    ids = [nid for nid, n in pcb.nets.items() if nid and n.name != 'FENCE']
    with contextlib.redirect_stdout(io.StringIO()):
        planned, unplaced = er.plan(pcb, cfg, ids)
    got = sorted(pcb.nets[nid].name for nid in planned)
    missed = sorted(pcb.nets[p.net_id].name for p in unplaced
                    if p.component_ref == 'U1')
    return got, missed


def main():
    d = tempfile.mkdtemp(prefix='t_er_tight_')
    errs = []
    try:
        cfg = _config()
        cases = [
            ('0.50 mm pitch, A B C', ['', 'SIG_A', 'SIG_B', 'SIG_C', ''], 0.50, []),
            ('0.65 mm pitch, A B C', ['', 'SIG_A', 'SIG_B', 'SIG_C', ''], 0.65,
             ['SIG_B']),
            ('0.50 mm pitch, - B -', ['', '', 'SIG_B', '', ''], 0.50, ['SIG_B']),
        ]
        for i, (label, pins, pitch, want) in enumerate(cases):
            got, missed = _plan(_load(d, f'c{i}.kicad_pcb', pins, pitch), cfg)
            print(f"  {label}: reserved {', '.join(got) or '-'}, no site "
                  f"{', '.join(missed) or '-'} (want reserved {', '.join(want) or '-'})")
            if got != want:
                errs.append(f"{label}: reserved {got}, want {want}")
            if pins[1] and missed != ['SIG_A', 'SIG_C']:
                errs.append(f"{label}: fixture broken, A and C must find no site "
                            f"(got {missed})")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    for e in errs:
        print(f"FAIL: {e}")
    print("PASS" if not errs else "FAILED")
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
