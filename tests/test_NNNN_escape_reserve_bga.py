#!/usr/bin/env python3
"""Escape reservation, BGA part: an inner ball whose only way out is one via
site between balls keeps that site when a net routed earlier would run its
track across it.

Fixture: a 5 x 5 BGA, 1.0 mm pitch, 0.55 mm balls on F.Cu (no track passes
between two balls, so an inner ball leaves only through a via at a diagonal
midpoint). Every ball is GND except the centre ball (B1) and its right
neighbour (B2). B.Cu GND pads sit under both balls (no via in the ball) and
on all their midpoints but two: SITE, shared, the centre ball's only one, and
ALT, the neighbour's own. B1 ends on a B.Cu pad to the right, B2 on one to
the lower left. Routed one net at a time, the first net's via or track takes
the other net's only site.

Checks:
  1. upstream behaviour: without the planner the main pass fails a net;
  2. with KICAD_ESCAPE_RESERVE=1 the planner reserves B1 at SITE and B2 at
     ALT (innermost ball first), the main pass fails no net and both nets
     connect.
"""
import contextlib
import io
import math
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))
from run_utils import evidence  # noqa: E402

PITCH = 1.0
BALL = 0.55
VIA, DRILL = 0.45, 0.2
X0, Y0 = 110.0, 110.0
SITE = (0.5, 0.5)                     # the centre ball's one legal midpoint
ALT = (1.5, 0.5)                      # the neighbour ball's other site
# B.Cu GND pads: under both signal balls (no via in the ball) and on every
# other midpoint of the two balls
BLOCKED = [(0, 0), (1, 0), (-0.5, -0.5), (-0.5, 0.5), (0.5, -0.5), (1.5, -0.5)]
NETS = ['B1', 'B2']


def _pad(num, x, y, sx, sy, net, name, shape='circle', layer='F.Cu'):
    return (f'\t\t(pad "{num}" smd {shape}\n\t\t\t(at {x:.4f} {y:.4f})\n'
            f'\t\t\t(size {sx} {sy})\n\t\t\t(layers "{layer}")\n'
            f'\t\t\t(net {net} "{name}")\n\t\t)\n')


def _fp(ref, x, y, pads, layer='F.Cu'):
    return (f'\t(footprint "test:{ref}"\n\t\t(layer "{layer}")\n'
            f'\t\t(at {x:.4f} {y:.4f})\n'
            f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 -5 0)\n'
            f'\t\t\t(layer "F.SilkS")\n'
            f'\t\t\t(effects (font (size 1 1) (thickness 0.15)))\n\t\t)\n'
            + ''.join(pads) + '\t)\n')


def board_text():
    nets = ['(net 0 "")', '(net 1 "GND")', '(net 2 "B1")', '(net 3 "B2")']
    u1 = []
    for r in range(5):
        for c in range(5):
            x, y = (c - 2) * PITCH, (r - 2) * PITCH
            num = f'{"ABCDE"[r]}{c + 1}'
            if r == 2 and c == 2:
                u1.append(_pad(num, x, y, BALL, BALL, 2, 'B1'))
            elif r == 2 and c == 3:
                u1.append(_pad(num, x, y, BALL, BALL, 3, 'B2'))
            else:
                u1.append(_pad(num, x, y, BALL, BALL, 1, 'GND'))
    blk = [_pad(str(i + 1), x, y, 0.5, 0.5, 1, 'GND', layer='B.Cu')
           for i, (x, y) in enumerate(BLOCKED)]
    body = (_fp('U1', X0, Y0, u1)
            + _fp('G1', X0, Y0, blk, 'B.Cu')
            + _fp('J1', X0 + 6.0, Y0 + SITE[1],
                  [_pad('1', 0, 0, 0.8, 0.8, 2, 'B1', layer='B.Cu')], 'B.Cu')
            + _fp('J2', X0 - 4.0, Y0 + 4.0,
                  [_pad('1', 0, 0, 0.8, 0.8, 3, 'B2', layer='B.Cu')], 'B.Cu'))
    return ('(kicad_pcb\n\t(version 20241229)\n\t(generator "test_NNNN_escape_reserve_bga")\n'
            '\t(general\n\t\t(thickness 1.6)\n\t)\n'
            '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(31 "B.Cu" signal)\n'
            '\t\t(44 "Edge.Cuts" user)\n\t)\n\t' + '\n\t'.join(nets) + '\n'
            + body +
            f'\t(gr_rect\n\t\t(start {X0 - 6:.4f} {Y0 - 6:.4f})\n'
            f'\t\t(end {X0 + 8:.4f} {Y0 + 6:.4f})\n'
            '\t\t(stroke (width 0.1) (type default))\n'
            '\t\t(layer "Edge.Cuts")\n\t)\n)\n')


def _write(d, name):
    p = os.path.join(d, name)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(board_text())
    return evidence(p)


def _config():
    from routing_config import GridRouteConfig
    return GridRouteConfig(track_width=0.2, clearance=0.15, via_size=VIA,
                           via_drill=DRILL, grid_step=0.05,
                           layers=['F.Cu', 'B.Cu'])


def _connected(path):
    from kicad_parser import parse_kicad_pcb
    from check_connected import check_net_connectivity
    pcb = parse_kicad_pcb(path)
    n = 0
    for nid, net in pcb.nets.items():
        if net.name not in NETS:
            continue
        r = check_net_connectivity(nid, [s for s in pcb.segments if s.net_id == nid],
                                   [v for v in pcb.vias if v.net_id == nid],
                                   pcb.pads_by_net.get(nid, []))
        n += bool(r.get('connected'))
    return n


def _route(src, out, on):
    import env_knobs
    import route
    saved = env_knobs.ESCAPE_RESERVE
    try:
        env_knobs.ESCAPE_RESERVE = on
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            route.batch_route(src, out, list(NETS), layers=['F.Cu', 'B.Cu'],
                              track_width=0.2, clearance=0.15, via_size=VIA,
                              via_drill=DRILL, grid_step=0.05)
    finally:
        env_knobs.ESCAPE_RESERVE = saved
    return buf.getvalue()


def check_plan(d):
    """The centre ball (innermost first) gets its one midpoint, the
    neighbour its other one."""
    from kicad_parser import parse_kicad_pcb
    import escape_reserve as er
    pcb = parse_kicad_pcb(_write(d, 'plan.kicad_pcb'))
    cfg = _config()
    ids = {n.name: nid for nid, n in pcb.nets.items() if n.name in NETS}
    planned, unplaced = er.plan(pcb, cfg, list(ids.values()))
    errs = []
    for name, site in (('B1', SITE), ('B2', ALT)):
        want = (X0 + site[0], Y0 + site[1])
        vias = [it[1] for it in planned.get(ids[name], [])]
        ok = len(vias) == 1 and math.hypot(vias[0][0] - want[0], vias[0][1] - want[1]) < 1e-3
        print(f"  plan: {name} via {vias} (want {want}): {'ok' if ok else 'NO'}")
        if not ok:
            errs.append(f"plan: {name} via {vias}, want one at {want}")
    if unplaced:
        errs.append(f"plan: {len(unplaced)} ball(s) without a site")
    return errs


def check_routing(d):
    src = _write(d, 'route.kicad_pcb')
    got = {}
    for on in (False, True):
        out = os.path.join(d, f'out{int(on)}.kicad_pcb')
        log = _route(src, out, on)
        got[on] = (log.count('FAILED: Could not find route'), _connected(evidence(out)))
        print(f"  planner {'on ' if on else 'off'}: main pass failed {got[on][0]}, "
              f"connected {got[on][1]}/{len(NETS)}")
    errs = []
    if got[False][0] == 0:
        errs.append("routing: the main pass routes both nets without the planner "
                    "(fixture no longer takes the ball's site)")
    if got[True][0]:
        errs.append(f"routing: the main pass failed {got[True][0]} net(s) with the planner")
    if got[True][1] != len(NETS):
        errs.append(f"routing: {got[True][1]}/{len(NETS)} connected with the planner")
    return errs


def main():
    d = tempfile.mkdtemp(prefix='tNNNN_erb_')
    try:
        errs = check_plan(d) + check_routing(d)
        if os.environ.get('T_ER_KEEP'):
            shutil.copytree(d, os.environ['T_ER_KEEP'], dirs_exist_ok=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    for e in errs:
        print(f"FAIL: {e}")
    print("PASS" if not errs else f"{len(errs)} failure(s)")
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
