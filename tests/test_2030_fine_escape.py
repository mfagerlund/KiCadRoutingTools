#!/usr/bin/env python3
"""Fine escape windows: a fine-pitch pad whose legal exit band holds no line
of the run's grid gets its escape stub routed at the rescue grid before the
main pass, and the net routes.

Fixture: a two-column part, 0.5 mm pitch, 1.2 x 0.3 mm pads, a centre pad
that seals the inside; every signal pad goes out to its own round connector
pad. At 0.25 mm track / 0.2 mm clearance the exit band of an inner pad is its
centre +-0.025 mm. With the part's pad centres on y = 0.05 mod 0.1 no 0.1 mm
grid line lies in it: the pad is GRID-BOXED and the 0.1 mm main pass cannot
leave it. Shifted by 0.05 mm (pad centres on the grid) nothing is boxed.

Checks:
  1. detection: 8 grid-boxed pads off-grid (the 4 inner pads of each
     column), 0 on-grid;
  2. routing off-grid: without the pre-pass the main pass fails the 8
     boxed nets (the fine-grid rescue recovers them later); with it the main
     pass routes all 12 and no net reaches the rescue. Every net connects
     either way.
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
ROWS = 6


def _pad(num, x, y, sx, sy, net, name, shape='roundrect'):
    return (f'\t\t(pad "{num}" smd {shape}\n\t\t\t(at {x:.4f} {y:.4f})\n'
            f'\t\t\t(size {sx} {sy})\n\t\t\t(layers "F.Cu")\n'
            f'\t\t\t(net {net} "{name}")\n\t\t)\n')


def _fp(ref, x, y, pads):
    return (f'\t(footprint "test:{ref}"\n\t\t(layer "F.Cu")\n'
            f'\t\t(at {x:.4f} {y:.4f})\n'
            f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 -5 0)\n'
            f'\t\t\t(layer "F.SilkS")\n'
            f'\t\t\t(effects (font (size 1 1) (thickness 0.15)))\n\t\t)\n'
            + ''.join(pads) + '\t)\n')


def board_text(y_off):
    nets = ['(net 0 "")', '(net 1 "GND")']
    u1, j1, j2 = [], [], []
    u1.append(_pad('EP', 0, 0, 2.4, 3.6, 1, 'GND', 'rect'))
    nid = 2
    for side, col in ((-1, j1), (1, j2)):
        for k in range(ROWS):
            name = f'S{nid - 1}'
            nets.append(f'(net {nid} "{name}")')
            ly = -(ROWS - 1) * PITCH / 2 + k * PITCH
            u1.append(_pad(str(nid), side * 2.0, ly, 1.2, 0.3, nid, name))
            col.append(_pad('1' if not col else str(len(col) + 1), 0,
                            -(ROWS - 1) * 0.75 + k * 1.5, 0.8, 0.8, nid, name,
                            'circle'))
            nid += 1
    body = (_fp('U1', 110, 110 + y_off, u1) + _fp('J1', 103, 110, j1)
            + _fp('J2', 117, 110, j2))
    return ('(kicad_pcb\n\t(version 20241229)\n\t(generator "test_2030")\n'
            '\t(general\n\t\t(thickness 1.6)\n\t)\n'
            '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(31 "B.Cu" signal)\n'
            '\t\t(44 "Edge.Cuts" user)\n\t)\n\t' + '\n\t'.join(nets) + '\n'
            + body +
            '\t(gr_rect\n\t\t(start 100 104)\n\t\t(end 120 116)\n'
            '\t\t(stroke (width 0.1) (type default))\n'
            '\t\t(layer "Edge.Cuts")\n\t)\n)\n')


def _write(d, name, y_off):
    p = os.path.join(d, name)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(board_text(y_off))
    return evidence(p)


def _config():
    from routing_config import GridRouteConfig
    return GridRouteConfig(track_width=0.25, clearance=0.2, via_size=0.8,
                           via_drill=0.4, grid_step=0.1,
                           layers=['F.Cu', 'B.Cu'])


def check_detection(d):
    from kicad_parser import parse_kicad_pcb
    from fine_escape import find_grid_boxed_pads
    errs = []
    for name, off, want in (('off.kicad_pcb', 0.0, 8), ('on.kicad_pcb', 0.05, 0)):
        pcb = parse_kicad_pcb(_write(d, name, off))
        ids = [nid for nid, n in pcb.nets.items() if n.name.startswith('S')]
        got = find_grid_boxed_pads(pcb, _config(), ids)
        print(f"  {name}: {len(got)} grid-boxed pad(s), want {want}")
        if len(got) != want:
            errs.append(f"detection {name}: {len(got)} grid-boxed, want {want}")
    return errs


def _connected(path):
    from kicad_parser import parse_kicad_pcb
    from check_connected import check_net_connectivity
    pcb = parse_kicad_pcb(path)
    n = 0
    for nid, net in pcb.nets.items():
        if not net.name.startswith('S'):
            continue
        r = check_net_connectivity(nid, [s for s in pcb.segments if s.net_id == nid],
                                   [v for v in pcb.vias if v.net_id == nid],
                                   pcb.pads_by_net.get(nid, []))
        n += bool(r.get('connected'))
    return n


def check_routing(d):
    import env_knobs
    import route
    src = _write(d, 'route.kicad_pcb', 0.0)
    names = [f'S{i}' for i in range(1, 2 * ROWS + 1)]
    got = {}
    saved = env_knobs.FINE_ESCAPE
    try:
        for on in (False, True):
            env_knobs.FINE_ESCAPE = on
            out = os.path.join(d, f'out{int(on)}.kicad_pcb')
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                route.batch_route(src, out, names, layers=['F.Cu', 'B.Cu'],
                                  track_width=0.25, clearance=0.2,
                                  via_size=0.8, via_drill=0.4, grid_step=0.1)
            log = buf.getvalue()
            got[on] = (log.count('FAILED: Could not find route'),
                       _connected(evidence(out)))
    finally:
        env_knobs.FINE_ESCAPE = saved
    for on in (False, True):
        print(f"  pre-pass {'on ' if on else 'off'}: main pass failed "
              f"{got[on][0]}, connected {got[on][1]}/{len(names)}")
    errs = []
    if got[False][0] != 8:
        errs.append(f"routing: main pass failed {got[False][0]} without the "
                    "pre-pass, want 8 (fixture no longer grid-boxed)")
    if got[True][0] != 0:
        errs.append(f"routing: main pass failed {got[True][0]} with the pre-pass")
    for on in (False, True):
        if got[on][1] != len(names):
            errs.append(f"routing: {got[on][1]}/{len(names)} connected "
                        f"(pre-pass {'on' if on else 'off'})")
    return errs


def main():
    d = tempfile.mkdtemp(prefix='t2030_')
    try:
        errs = check_detection(d) + check_routing(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    for e in errs:
        print(f"FAIL: {e}")
    print("PASS" if not errs else f"{len(errs)} failure(s)")
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
