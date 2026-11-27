#!/usr/bin/env python3
"""KICAD_ESCAPE_NECK: a signal net whose width cannot leave a fine-pitch pin
enters it over a short neck at the board's minimum track width.

The neck-down ladder serves power and impedance nets only, so a signal net
routed at 0.25 mm never leaves a pin boxed in by 0.4 mm gaps, though the
board allows a far narrower track. The escape reservation finds no legal
dog-bone at the net's width, the main pass gives the edge up, and only the
late per-net rescue reconnects it, narrow from end to end, on whatever room
the finished board has left. With the knob on, escape_reserve retries the
dog-bone candidates with the stub alone at the fab floor / rule minimum
(fine_escape.escape_neck_width) and the main pass carries on from it at the
net's width.

Fixture (generated): a five-row part, 0.4 mm square pads at 0.8 mm pitch,
alternate rows offset half a pitch, so the only way out of a second-row pin
toward the far pad is a 0.4 mm gap in the outer row (a 0.25 mm track at
0.1 mm clearance needs 0.45). That pin is on SIG with a 1 mm pad 6 mm north;
the other pins carry their own nets. B.Cu copper of another net under the
part leaves no via site beside the pin. F.Cu + B.Cu, SIG routed at
--track-width 0.25 --clearance 0.1, KICAD_ESCAPE_RESERVE=1 in both runs.

Checks:
  1. The knob is opt-in (off by default).
  2. Knob off: the main pass fails the edge (the late rescue may reconnect
     it, narrow end to end: the behaviour this fixes).
  3. Knob on: the preflight line, the neck line, no rescue needed, all pads
     connected, DRC clean, the trunk keeps 0.25 mm, nothing below 0.09 mm,
     and every piece under 0.25 mm lies within PAD_ENTRY_NECK_MM of a SIG pad.

    python3 tests/test_NNNN_escape_neck.py
"""
import json
import math
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'rust_router', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _d))

from kicad_parser import parse_kicad_pcb                     # noqa: E402
from routing_defaults import PAD_ENTRY_NECK_MM               # noqa: E402
from run_utils import check as run_check, evidence, tool, tool_env  # noqa: E402

RUN_ALL_TIMEOUT = 300
NET_W, CLEARANCE = 0.25, 0.1
PITCH, PAD = 0.8, 0.4
fails = []


def check(name, ok, detail=''):
    print(('PASS' if ok else 'FAIL') + f': {name}' + (f' -- {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def make_board(path):
    """The boxed-pin part and SIG's far pad (see the module docstring)."""
    nets, pads = ['SIG'], []
    rows = [((r - 2) * PITCH, -4.0 if r % 2 == 0 else -3.6, 11 - r % 2)
            for r in range(5)]
    for row, (dy, x0, n) in enumerate(rows):
        for i in range(n):
            x = round(x0 + i * PITCH, 4)
            if row == 1 and abs(x - 0.4) < 1e-6:
                name = 'SIG'
            else:
                name = f'N{len(nets)}'
                nets.append(name)
            pads.append((f'{row}{i}', x, round(dy, 4), PAD, name))
    nets.append('BLK')
    ids = {n: i + 1 for i, n in enumerate(nets)}

    def fp(ref, at, plist, layer='F'):
        body = ''.join(
            f'\t\t(pad "{num}" smd rect\n\t\t\t(at {x} {y})\n\t\t\t(size {sx} {sy})\n'
            f'\t\t\t(layers "{layer}.Cu" "{layer}.Mask")\n\t\t\t(net {ids[n]} "{n}")\n'
            f'\t\t\t(uuid "{ref}-{num}")\n\t\t)\n'
            for num, x, y, sx, sy, n in ((q[0], q[1], q[2], q[3], q[3], q[4])
                                         if len(q) == 5 else q for q in plist))
        return (f'\t(footprint "test:FP"\n\t\t(layer "F.Cu")\n\t\t(uuid "{ref}")\n'
                f'\t\t(at {at[0]} {at[1]})\n\t\t(property "Reference" "{ref}"\n'
                f'\t\t\t(at 0 0)\n\t\t)\n{body}\t)\n')

    text = ['(kicad_pcb\n\t(version 20241229)\n\t(generator "synth")\n'
            '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(2 "B.Cu" signal)\n'
            '\t\t(25 "Edge.Cuts" user)\n\t)\n\t(net 0 "")\n']
    text += [f'\t(net {i} "{n}")\n' for n, i in ids.items()]
    text.append('\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 20 20)\n'
                '\t\t(layer "Edge.Cuts")\n\t\t(uuid "edge")\n\t)\n')
    text.append(fp('U1', (10, 12), pads))
    text.append(fp('J1', (10.4, 6), [('1', 0, 0, 1.0, 'SIG')]))
    # B.Cu copper of another net under the part, short of the outer row's
    # far edge: no via fits beside the pin, only past the outer row.
    text.append(fp('X1', (10, 12.15), [('1', 0, 0, 9.6, 3.3, 'BLK')], 'B'))
    text.append(')\n')
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(''.join(text))


def route(tmp, board, knob):
    out = os.path.join(tmp, f'out_{int(knob)}.kicad_pcb')
    env = dict(os.environ)
    env.pop('KICAD_ESCAPE_NECK', None)
    env['KICAD_ESCAPE_RESERVE'] = '1'
    if knob:
        env['KICAD_ESCAPE_NECK'] = '1'
    r = run_check([sys.executable, '-X', 'utf8', tool('route.py'), board, out,
                   '--nets', 'SIG', '--layers', 'F.Cu', 'B.Cu',
                   '--track-width', str(NET_W), '--clearance', str(CLEARANCE),
                   '--via-size', '0.5', '--via-drill', '0.3'],
                  accept=True, env=tool_env(env))
    log = r.stdout + r.stderr
    summary = {}
    for line in log.splitlines():
        if line.startswith('JSON_SUMMARY: '):
            summary = json.loads(line[len('JSON_SUMMARY: '):])
    return evidence(out, 'routed board'), log, summary


def seg_len(s):
    return math.hypot(s.end_x - s.start_x, s.end_y - s.start_y)


def connected(out):
    r = run_check([sys.executable, '-X', 'utf8', tool('check_connected.py'), out,
                   '--nets', 'SIG'], accept=True)
    return 'ALL NETS FULLY CONNECTED' in r.stdout, r.stdout[-300:]


print("1. opt-in")
saved = os.environ.pop('KICAD_ESCAPE_NECK', None)
try:
    import env_knobs
    check("ESCAPE_NECK is off by default", not env_knobs.ESCAPE_NECK)
finally:
    if saved is not None:
        os.environ['KICAD_ESCAPE_NECK'] = saved

tmp = tempfile.mkdtemp(prefix='tescneck_')
try:
    board = os.path.join(tmp, 'boxed.kicad_pcb')
    make_board(board)

    print("2. knob off: the main pass fails the edge")
    out, log, summ = route(tmp, board, False)
    rescue = summ.get('rescue') or {}
    check("the main pass leaves SIG to the rescue (or open)",
          bool(rescue.get('attempted')) or not connected(out)[0],
          f"rescue={rescue.get('attempted')}")
    check("no escape-neck line without the knob", 'Escape neck' not in log)

    print("3. knob on")
    out, log, summ = route(tmp, board, True)
    check("preflight: the log says the escape neck is armed",
          'Escape neck: armed' in log)
    check("the log names the neck on the edge",
          'Escape neck: U1.' in log and 'stub at' in log)
    check("no rescue needed", not (summ.get('rescue') or {}).get('attempted'),
          str((summ.get('rescue') or {}).get('attempted')))
    ok, txt = connected(out)
    check("all SIG pads connected (check_connected)", ok, txt)
    r = run_check([sys.executable, '-X', 'utf8', tool('check_drc.py'), out,
                   '--clearance', str(CLEARANCE)], accept=True)
    check("DRC clean at the routed clearance", 'NO DRC VIOLATIONS' in r.stdout,
          r.stdout[-300:])
    pcb = parse_kicad_pcb(out)
    sig = next(n for n, net in pcb.nets.items() if net.name == 'SIG')
    segs = [s for s in pcb.segments if s.net_id == sig]
    pads = list(pcb.pads_by_net.get(sig, []))
    check("the trunk keeps the net's width",
          any(abs(s.width - NET_W) < 1e-6 for s in segs))
    narrow = [s for s in segs if s.width < NET_W - 1e-6]
    check("some copper is necked", bool(narrow))
    check("no zero/absurd widths", bool(segs) and min(s.width for s in segs) >= 0.09,
          str(min((s.width for s in segs), default=None)))
    print(f"   under {NET_W} mm: {sum(seg_len(s) for s in narrow):.3f} mm in "
          f"{len(narrow)} piece(s): "
          + ', '.join(f"{s.width:g}x{seg_len(s):.2f}" for s in narrow))
    far = [s for s in narrow
           if min(max(math.hypot(s.start_x - p.global_x, s.start_y - p.global_y),
                      math.hypot(s.end_x - p.global_x, s.end_y - p.global_y))
                  for p in pads) > PAD_ENTRY_NECK_MM + 1e-6]
    check("every narrow piece lies within the allowance of a SIG pad",
          not far, str([(s.start_x, s.start_y, s.end_x, s.end_y) for s in far]))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print(f"FAILED {len(fails)}: " + '; '.join(fails))
    sys.exit(1)
print("ALL PASS")
