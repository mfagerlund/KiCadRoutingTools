#!/usr/bin/env python3
"""C10 regression: plane-repair reconnects must keep vias off same-net pads.

Seance (C10 repo, 2026-10-13): `route.py ... --same-net-pad-clearance 0.12`
shipped three vias inside same-net pads (C306.2, R312.2, U301.8, all on
ADC_AIN2N). The main pass honoured the flag. The in-run plane finalize (#562)
then repaired a GND pad by ripping ADC_AIN2N, and repair_planes' immediate
reconnect re-routed it through `batch_route(...)` WITHOUT
`same_net_pad_clearance`. batch_route's function path then auto-reads the
project record beside `input_file`, which mid-run does not exist yet (the
record is persisted into the OUTPUT .kicad_pro after the run), so the
sub-route ran with the constraint off and dropped its layer changes into the
pads.

Two halves:
1. Real A* through batch_route on a tiny board whose cheapest layer change is
   a via at the pad centre: with the clearance omitted and no project record
   (what the reconnect sub-route saw) the via lands in the pad; forwarded,
   it stays off. This is the mechanism.
2. Every batch_route sub-call in the plane engines forwards the clearance
   explicitly (a `**_ghost_kwargs(...)` spread is not wholesale forwarding).

    python tests/test_c10_repair_reconnect_same_net_pad.py
"""
import ast
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ('', 'py_router', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, sub))

from kicad_parser import parse_kicad_pcb  # noqa: E402

GAP = 0.12


def _board():
    # SIG: an F.Cu pad and a B.Cu pad at the same spot. The shortest route is a
    # single via at the pad centre; with the clearance active it must dog-bone.
    edge = "\n".join(
        f' (gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (layer "Edge.Cuts") (width 0.1))'
        for a, b in (((0, 0), (20, 0)), ((20, 0), (20, 20)), ((20, 20), (0, 20)), ((0, 20), (0, 0))))
    return ('(kicad_pcb (version 20221018) (generator test)\n'
            ' (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
            ' (net 0 "")\n (net 1 "SIG")\n'
            f'{edge}\n'
            ' (footprint "test:R1" (layer "F.Cu") (at 10 10)\n'
            '  (property "Reference" "R1" (at 0 -2) (layer "F.SilkS"))\n'
            '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG")))\n'
            ' (footprint "test:R2" (layer "B.Cu") (at 10 10)\n'
            '  (property "Reference" "R2" (at 0 2) (layer "B.SilkS"))\n'
            '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "B.Cu" "B.Paste" "B.Mask") (net 1 "SIG")))\n'
            ')\n')


def _route(tmp, gap):
    from route import batch_route
    inp = os.path.join(tmp, 'in.kicad_pcb')
    outp = os.path.join(tmp, f'out-{gap}.kicad_pcb')
    with open(inp, 'w', encoding='utf-8') as f:
        f.write(_board())
    batch_route(inp, outp, ['SIG'], layers=['F.Cu', 'B.Cu'], track_width=0.12, clearance=0.12,
                via_size=0.45, via_drill=0.2, grid_step=0.05, same_net_pad_clearance=gap)
    return parse_kicad_pcb(outp).vias


def _in_pad(v):
    # Pad copper is the 1 x 1 mm square at (10, 10); barrel radius 0.225 mm.
    dx, dy = max(abs(v.x - 10) - .5, 0), max(abs(v.y - 10) - .5, 0)
    return (dx * dx + dy * dy) ** .5 - v.size / 2


def _calls(path):
    tree = ast.parse(open(path, encoding='utf-8').read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (f.id if isinstance(f, ast.Name) else getattr(f, 'attr', None)) == 'batch_route':
                yield node


def main():
    failures = []

    def check(name, ok):
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
        if not ok:
            failures.append(name)

    tmp = tempfile.mkdtemp()
    legacy = _route(tmp, None)
    check('repro: omitted clearance, no project record -> a via inside the same-net pad',
          bool(legacy) and any(_in_pad(v) < 0 for v in legacy))
    fixed = _route(tmp, GAP)
    check('forwarded clearance -> the layer change still routes', bool(fixed))
    check(f'forwarded clearance -> every via edge >= {GAP} mm from the same-net pad',
          bool(fixed) and all(_in_pad(v) >= GAP - 1e-6 for v in fixed))

    missing, found = [], 0
    for rel in ('py_router/repair_planes.py', 'py_router/route_planes.py'):
        for call in _calls(os.path.join(ROOT, rel)):
            found += 1
            if 'same_net_pad_clearance' not in {k.arg for k in call.keywords if k.arg}:
                missing.append(f'{rel}:{call.lineno}')
    check('plane-engine batch_route sub-calls found', found >= 3)
    check('every plane-engine batch_route sub-call forwards same_net_pad_clearance'
          + (f' (missing: {", ".join(missing)})' if missing else ''), not missing)
    print(f'{len(failures)} failure(s)')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
