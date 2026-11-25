#!/usr/bin/env python3
"""Escape reservation, row part: the via sites in front of a fine-pitch row
are planned for every pin together before routing, so the first nets to
route cannot take the sites their neighbours need.

Fixture: a single column of N signal pads, 0.5 mm pitch, 0.8 x 0.25 mm, on
F.Cu with an exposed pad behind them (the row exits to +x). Every signal net
ends on its own B.Cu-only pad of a far connector, so every net needs one via.
Between the row and the connector an F.Cu-only wall pad of another net
leaves a strip of STRIP mm in front of the row; the board edge closes the
strip at both ends. The strip holds one via per pin only when the vias
stagger and fan: a net routed alone drops its via where its own path is
shortest, in the middle of the strip, and the later neighbours find no site.

Checks:
  1. upstream behaviour: without the planner the main pass fails nets of
     the row (their sites were taken by earlier nets; rip-up may recover
     them later, at the cost of extra passes);
  2. with KICAD_ESCAPE_RESERVE=1 the planner reserves one dog-bone (stub
     and via) per pin, every reservation is legal against the others, the
     main pass routes every net from its via (no net fails) and every net
     connects;
  3. a net that never routes has its reservation released after the
     passes: no stub or via of an unrouted net ships;
  4. the planner is opt-in: KICAD_ESCAPE_RESERVE unset leaves it off.
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

PITCH = 0.5
N = int(os.environ.get('T_ER_N', '8'))
STRIP = float(os.environ.get('T_ER_STRIP', '1.8'))
VIA, DRILL = 0.6, 0.3
ROW_X = 110.0


def _pad(num, x, y, sx, sy, net, name, shape='roundrect', layer='F.Cu'):
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


def board_text(n=N, strip=STRIP):
    nets = ['(net 0 "")', '(net 1 "GND")', '(net 2 "WALL")']
    span = (n - 1) * PITCH
    u1 = [_pad('EP', -1.6, 0, 2.0, span + 0.6, 1, 'GND', 'rect')]
    j1 = []
    for k in range(n):
        nid = 3 + k
        name = f'S{k + 1}'
        nets.append(f'(net {nid} "{name}")')
        y = -span / 2 + k * PITCH
        u1.append(_pad(str(k + 1), 0, y, 0.8, 0.25, nid, name))
        j1.append(_pad(str(k + 1), 0, -(n - 1) * 0.6 + k * 1.2, 0.8, 0.8, nid,
                       name, 'circle', 'B.Cu'))
    half_h = max(span / 2 + 1.2, (n - 1) * 0.6 + 1.0)
    row_edge = ROW_X + 0.4
    wall_x0 = row_edge + strip
    wall_x1 = ROW_X + 7.0
    wall = [_pad('1', 0, 0, wall_x1 - wall_x0, 2 * half_h + 2, 2, 'WALL', 'rect')]
    body = (_fp('U1', ROW_X, 110, u1) + _fp('J1', ROW_X + 9.0, 110, j1, 'B.Cu')
            + _fp('W1', (wall_x0 + wall_x1) / 2, 110, wall))
    return ('(kicad_pcb\n\t(version 20241229)\n\t(generator "test_NNNN_escape_reserve_qfn")\n'
            '\t(general\n\t\t(thickness 1.6)\n\t)\n'
            '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(31 "B.Cu" signal)\n'
            '\t\t(44 "Edge.Cuts" user)\n\t)\n\t' + '\n\t'.join(nets) + '\n'
            + body +
            f'\t(gr_rect\n\t\t(start {ROW_X - 4:.4f} {110 - half_h:.4f})\n'
            f'\t\t(end {ROW_X + 11:.4f} {110 + half_h:.4f})\n'
            '\t\t(stroke (width 0.1) (type default))\n'
            '\t\t(layer "Edge.Cuts")\n\t)\n)\n')


def _write(d, name, **kw):
    p = os.path.join(d, name)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(board_text(**kw))
    return evidence(p)


def _config():
    from routing_config import GridRouteConfig
    return GridRouteConfig(track_width=0.2, clearance=0.15, via_size=VIA,
                           via_drill=DRILL, grid_step=0.05,
                           layers=['F.Cu', 'B.Cu'])


def _connected(path, prefix='S'):
    from kicad_parser import parse_kicad_pcb
    from check_connected import check_net_connectivity
    pcb = parse_kicad_pcb(path)
    n = 0
    for nid, net in pcb.nets.items():
        if not net.name.startswith(prefix):
            continue
        r = check_net_connectivity(nid, [s for s in pcb.segments if s.net_id == nid],
                                   [v for v in pcb.vias if v.net_id == nid],
                                   pcb.pads_by_net.get(nid, []))
        n += bool(r.get('connected'))
    return n


def _route(src, out, names, on):
    import env_knobs
    import route
    saved = env_knobs.ESCAPE_RESERVE
    try:
        env_knobs.ESCAPE_RESERVE = on
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            route.batch_route(src, out, names, layers=['F.Cu', 'B.Cu'],
                              track_width=0.2, clearance=0.15, via_size=VIA,
                              via_drill=DRILL, grid_step=0.05)
    finally:
        env_knobs.ESCAPE_RESERVE = saved
    return buf.getvalue()


def check_plan(d):
    """Every pin gets a dog-bone; the vias keep via + clearance apart."""
    from kicad_parser import parse_kicad_pcb
    import escape_reserve as er
    pcb = parse_kicad_pcb(_write(d, 'plan.kicad_pcb'))
    cfg = _config()
    ids = [nid for nid, n in pcb.nets.items() if n.name.startswith('S')]
    planned, unplaced = er.plan(pcb, cfg, ids)
    vias = [it[1] for items in planned.values() for it in items]
    close = [(a, b) for i, a in enumerate(vias) for b in vias[i + 1:]
             if math.hypot(a[0] - b[0], a[1] - b[1]) < VIA + cfg.clearance - 1e-6]
    print(f"  plan: {len(vias)} dog-bone(s) for {N} pins, {len(unplaced)} without "
          f"a site, {len(close)} via pair(s) too close")
    errs = []
    if len(vias) != N or unplaced or close:
        errs.append(f"plan: {len(vias)}/{N} dog-bones, {len(unplaced)} unplaced, "
                    f"{len(close)} close pairs")
    return errs


def check_routing(d):
    src = _write(d, 'route.kicad_pcb')
    names = [f'S{i}' for i in range(1, N + 1)]
    got = {}
    for on in (False, True):
        out = os.path.join(d, f'out{int(on)}.kicad_pcb')
        log = _route(src, out, names, on)
        got[on] = (log.count('FAILED: Could not find route'), _connected(evidence(out)))
        print(f"  planner {'on ' if on else 'off'}: main pass failed {got[on][0]}, "
              f"connected {got[on][1]}/{N}")
    errs = []
    if got[False][0] == 0:
        errs.append("routing: the main pass routes every net without the planner "
                    "(fixture no longer steals via sites)")
    if got[True][0]:
        errs.append(f"routing: the main pass failed {got[True][0]} net(s) with "
                    "the planner (a reserved via site was taken)")
    if got[True][1] != N:
        errs.append(f"routing: {got[True][1]}/{N} connected with the planner")
    return errs


def check_release(d):
    """A net that never routes leaves no reserved copper behind."""
    from kicad_parser import parse_kicad_pcb
    import escape_reserve as er
    import fine_escape as fe
    pcb = parse_kicad_pcb(_write(d, 'rel.kicad_pcb'))
    cfg = _config()
    ids = [nid for nid, n in pcb.nets.items() if n.name.startswith('S')]
    with contextlib.redirect_stdout(io.StringIO()):
        er.escape_reserve_prepass(pcb, cfg, ids)
    st = getattr(pcb, fe._STATE)
    a = sorted(st.pending)[0]
    objs = list(st.pending[a])
    errs = []
    if not any(fe._is_via(o) for o in objs) or not all(
            any(o is v for v in pcb.vias) or any(o is s for s in pcb.segments)
            for o in objs):
        errs.append("release: reservation holds no via or is not on the board")
    tok = fe.begin(pcb, a, cfg)
    fe.finish(pcb, tok, {'failed': True})
    if st.pending.get(a) != objs:
        errs.append("release: a failed route dropped its reservation")
    b = sorted(st.pending)[1]
    objs_b = list(st.pending[b])
    tok = fe.begin(pcb, b, cfg)
    res = fe.finish(pcb, tok, {'new_segments': [], 'new_vias': []})
    if (b in st.pending or not res.get('new_vias')
            or len(res['new_vias']) + len(res['new_segments']) != len(objs_b)):
        errs.append("release: a successful route did not take its stub and via")
    with contextlib.redirect_stdout(io.StringIO()):
        fe.release(pcb)
    left = [o for o in list(pcb.segments) + list(pcb.vias) if o.net_id in ids]
    if st.pending or left:
        errs.append(f"release: {len(left)} reserved item(s) left after release")
    print(f"  release: fail keeps, success takes stub+via, release clears: "
          f"{'ok' if not errs else 'NO'}")
    return errs


def check_opt_in():
    import env_knobs
    saved = os.environ.pop('KICAD_ESCAPE_RESERVE', None)
    try:
        env_knobs.refresh()
        off = env_knobs.ESCAPE_RESERVE
        os.environ['KICAD_ESCAPE_RESERVE'] = '1'
        env_knobs.refresh()
        on = env_knobs.ESCAPE_RESERVE
    finally:
        os.environ.pop('KICAD_ESCAPE_RESERVE', None)
        if saved is not None:
            os.environ['KICAD_ESCAPE_RESERVE'] = saved
        env_knobs.refresh()
    errs = []
    if off:
        errs.append("KICAD_ESCAPE_RESERVE unset arms the planner, want opt-in")
    if not on:
        errs.append("KICAD_ESCAPE_RESERVE=1 does not arm the planner")
    return errs


def main():
    d = tempfile.mkdtemp(prefix='tNNNN_erq_')
    try:
        errs = check_opt_in() + check_plan(d) + check_release(d) + check_routing(d)
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
