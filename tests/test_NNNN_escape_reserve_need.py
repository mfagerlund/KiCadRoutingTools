#!/usr/bin/env python3
"""Escape reservation plans only the row pins that need a via.

A fine-pitch row pin whose net ends on pads of the pin's own layer leaves
the row by a stub on that layer: a dog-bone reserved for it is copper it
does not need, and while it is reserved it blocks its neighbours' exits.
Only pins whose net cannot finish on their layer (every other pad of the net
is on another layer) get a reservation.

Fixture: a single column of N signal pads, 0.5 mm pitch, on F.Cu, with an
exposed GND pad behind them. Net k ends on a pad of a far connector: on
F.Cu for even k, on B.Cu for odd k.

Checks:
  1. the planner reserves dog-bones for exactly the B.Cu-ending nets;
  2. a net whose far pad is through-hole (reachable on every layer) gets
     no reservation;
  3. release() drops the bracket's reference to the routing state, so the
     pass's obstacle maps do not outlive routing.
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

PITCH = 0.5
N = 8
VIA, DRILL = 0.6, 0.3
ROW_X = 110.0


def _smd(num, x, y, sx, sy, net, name, shape, layer):
    return (f'\t\t(pad "{num}" smd {shape}\n\t\t\t(at {x:.4f} {y:.4f})\n'
            f'\t\t\t(size {sx} {sy})\n\t\t\t(layers "{layer}")\n'
            f'\t\t\t(net {net} "{name}")\n\t\t)\n')


def _tht(num, x, y, net, name):
    return (f'\t\t(pad "{num}" thru_hole circle\n\t\t\t(at {x:.4f} {y:.4f})\n'
            f'\t\t\t(size 0.9 0.9)\n\t\t\t(drill 0.5)\n'
            f'\t\t\t(layers "*.Cu" "*.Mask")\n'
            f'\t\t\t(net {net} "{name}")\n\t\t)\n')


def _fp(ref, x, y, pads, layer='F.Cu'):
    return (f'\t(footprint "test:{ref}"\n\t\t(layer "{layer}")\n'
            f'\t\t(at {x:.4f} {y:.4f})\n'
            f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 -5 0)\n'
            f'\t\t\t(layer "F.SilkS")\n'
            f'\t\t\t(effects (font (size 1 1) (thickness 0.15)))\n\t\t)\n'
            + ''.join(pads) + '\t)\n')


def board_text(far):
    """`far(k)` -> 'F', 'B' or 'T' (through-hole) for net k's far pad."""
    nets = ['(net 0 "")', '(net 1 "GND")']
    span = (N - 1) * PITCH
    u1 = [_smd('EP', -1.6, 0, 2.0, span + 0.6, 1, 'GND', 'rect', 'F.Cu')]
    j1 = []
    for k in range(N):
        nid = 2 + k
        name = f'S{k + 1}'
        nets.append(f'(net {nid} "{name}")')
        u1.append(_smd(str(k + 1), 0, -span / 2 + k * PITCH, 0.8, 0.25, nid,
                       name, 'roundrect', 'F.Cu'))
        y = -(N - 1) * 0.75 + k * 1.5
        kind = far(k)
        j1.append(_tht(str(k + 1), 0, y, nid, name) if kind == 'T' else
                  _smd(str(k + 1), 0, y, 0.8, 0.8, nid, name, 'circle',
                       'F.Cu' if kind == 'F' else 'B.Cu'))
    half_h = (N - 1) * 0.75 + 1.5
    body = _fp('U1', ROW_X, 110, u1) + _fp('J1', ROW_X + 9.0, 110, j1)
    return ('(kicad_pcb\n\t(version 20241229)\n\t(generator "test_NNNN_escape_reserve_need")\n'
            '\t(general\n\t\t(thickness 1.6)\n\t)\n'
            '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(31 "B.Cu" signal)\n'
            '\t\t(44 "Edge.Cuts" user)\n\t)\n\t' + '\n\t'.join(nets) + '\n'
            + body +
            f'\t(gr_rect\n\t\t(start {ROW_X - 4:.4f} {110 - half_h:.4f})\n'
            f'\t\t(end {ROW_X + 11:.4f} {110 + half_h:.4f})\n'
            '\t\t(stroke (width 0.1) (type default))\n'
            '\t\t(layer "Edge.Cuts")\n\t)\n)\n')


def _load(d, name, far):
    from kicad_parser import parse_kicad_pcb
    p = os.path.join(d, name)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(board_text(far))
    return parse_kicad_pcb(evidence(p))


def _config():
    from routing_config import GridRouteConfig
    return GridRouteConfig(track_width=0.2, clearance=0.15, via_size=VIA,
                           via_drill=DRILL, grid_step=0.05,
                           layers=['F.Cu', 'B.Cu'])


def _planned_names(pcb):
    import escape_reserve as er
    ids = [nid for nid, n in pcb.nets.items() if n.name.startswith('S')]
    planned, _ = er.plan(pcb, _config(), ids)
    return sorted(pcb.nets[nid].name for nid in planned)


def check_need(d):
    pcb = _load(d, 'need.kicad_pcb', lambda k: 'F' if k % 2 == 0 else 'B')
    got = _planned_names(pcb)
    want = sorted(f'S{k + 1}' for k in range(N) if k % 2)
    print(f"  reserved: {', '.join(got) or '-'} (want {', '.join(want)})")
    if got != want:
        return [f"need: reserved {got}, want only the B.Cu-ending nets {want}"]
    return []


def check_tht(d):
    pcb = _load(d, 'tht.kicad_pcb', lambda k: 'T')
    got = _planned_names(pcb)
    print(f"  through-hole far pads: reserved {', '.join(got) or 'none'}")
    if got:
        return [f"tht: nets ending on through-hole pads got reservations: {got}"]
    return []


def check_release_maps(d):
    import escape_reserve as er
    import fine_escape as fe
    pcb = _load(d, 'rel.kicad_pcb', lambda k: 'B')
    cfg = _config()
    ids = [nid for nid, n in pcb.nets.items() if n.name.startswith('S')]
    with contextlib.redirect_stdout(io.StringIO()):
        er.escape_reserve_prepass(pcb, cfg, ids)
        fe.bind_maps(pcb, object(), cfg)
        fe.release(pcb)
    st = getattr(pcb, fe._STATE)
    print(f"  after release: routing state {'held' if st.maps is not None else 'dropped'}")
    if st.maps is not None:
        return ["release: the bracket still holds the routing state after the passes"]
    return []


def main():
    d = tempfile.mkdtemp(prefix='t_er_need_')
    try:
        errs = check_need(d) + check_tht(d) + check_release_maps(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    for e in errs:
        print(f"FAIL: {e}")
    print("PASS" if not errs else "FAILED")
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
