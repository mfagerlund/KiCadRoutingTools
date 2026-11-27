#!/usr/bin/env python3
"""KICAD_FINE_NET_FLOOR: a signal net with a fine-pitch pin routes at the
board's minimum track width; every other net keeps its own width.

A signal net routed at 0.25 mm never leaves a pin boxed in by 0.4 mm gaps,
though the board allows a far narrower track: the main pass gives the edge
up and only the late per-net rescue reconnects it, on whatever room the
finished board has left. With the knob on, the run's reserve width drops to
the board floor, the fine-pin net routes at it, and the other nets are
pinned to their old width (held clear by their own track margin).

Fixture (generated): a five-row part, 0.4 mm square pads at 0.8 mm pitch,
alternate rows offset half a pitch, so the only way out of a second-row pin
toward the far pad is a 0.4 mm gap in the outer row (0.25 mm at 0.1 mm
clearance needs 0.45). That pin is on SIG with a 1 mm pad 6 mm north; the
other pins carry their own nets. B.Cu copper of another net under the part
leaves no via site beside the pin. WIDE runs between two pads in open
board. F.Cu + B.Cu, --track-width 0.25 --clearance 0.1, KICAD_ESCAPE_RESERVE=1
in both runs (the escape reservation plans the stub at the net's width).

Checks:
  1. The knob is opt-in (off by default).
  2. Knob off: the main pass fails SIG (the rescue may reconnect it).
  3. Knob on: the preflight line, no rescue, all pads connected, DRC clean,
     SIG copper at the 2-layer floor (0.127 mm), WIDE copper all 0.25 mm.

    python3 tests/test_NNNN_fine_net_floor.py
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'rust_router', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _d))

from kicad_parser import parse_kicad_pcb                     # noqa: E402
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
    nets += ['BLK', 'WIDE']
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
    text.append(fp('J2', (3, 3), [('1', 0, 0, 1.0, 'WIDE')]))
    text.append(fp('J3', (17, 3), [('1', 0, 0, 1.0, 'WIDE')]))
    text.append(')\n')
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(''.join(text))


def route(tmp, board, knob):
    out = os.path.join(tmp, f'out_{int(knob)}.kicad_pcb')
    env = dict(os.environ)
    env.pop('KICAD_FINE_NET_FLOOR', None)
    env['KICAD_ESCAPE_RESERVE'] = '1'
    if knob:
        env['KICAD_FINE_NET_FLOOR'] = '1'
    r = run_check([sys.executable, '-X', 'utf8', tool('route.py'), board, out,
                   '--nets', 'SIG', 'WIDE', '--layers', 'F.Cu', 'B.Cu',
                   '--track-width', str(NET_W), '--clearance', str(CLEARANCE),
                   '--via-size', '0.5', '--via-drill', '0.3'],
                  accept=True, env=tool_env(env))
    log = r.stdout + r.stderr
    summary = {}
    for line in log.splitlines():
        if line.startswith('JSON_SUMMARY: '):
            summary = json.loads(line[len('JSON_SUMMARY: '):])
    return evidence(out, 'routed board'), log, summary


def connected(out):
    r = run_check([sys.executable, '-X', 'utf8', tool('check_connected.py'), out,
                   '--nets', 'SIG', 'WIDE'], accept=True)
    return 'ALL NETS FULLY CONNECTED' in r.stdout, r.stdout[-300:]


print("1. opt-in")
saved = os.environ.pop('KICAD_FINE_NET_FLOOR', None)
try:
    import env_knobs
    check("FINE_NET_FLOOR is off by default", not env_knobs.FINE_NET_FLOOR)
finally:
    if saved is not None:
        os.environ['KICAD_FINE_NET_FLOOR'] = saved

tmp = tempfile.mkdtemp(prefix='tfinefloor_')
try:
    board = os.path.join(tmp, 'boxed.kicad_pcb')
    make_board(board)

    print("2. knob off: the main pass fails the edge")
    out, log, summ = route(tmp, board, False)
    rescue = summ.get('rescue') or {}
    check("the main pass leaves SIG to the rescue (or open)",
          bool(rescue.get('attempted')) or not connected(out)[0],
          f"rescue={rescue.get('attempted')}")
    check("no fine-net-floor line without the knob", 'Fine-net floor' not in log)

    print("3. knob on")
    out, log, summ = route(tmp, board, True)
    check("preflight: the log says the fine-net floor is armed with one net",
          'Fine-net floor: armed (KICAD_FINE_NET_FLOOR): 1 fine-pin' in log)
    check("no rescue needed", not (summ.get('rescue') or {}).get('attempted'),
          str((summ.get('rescue') or {}).get('attempted')))
    ok, txt = connected(out)
    check("all pads connected (check_connected)", ok, txt)
    r = run_check([sys.executable, '-X', 'utf8', tool('check_drc.py'), out,
                   '--clearance', str(CLEARANCE)], accept=True)
    check("DRC clean at the routed clearance", 'NO DRC VIOLATIONS' in r.stdout,
          r.stdout[-300:])
    pcb = parse_kicad_pcb(out)

    def widths(name):
        nid = next(n for n, net in pcb.nets.items() if net.name == name)
        return sorted({round(s.width, 4) for s in pcb.segments if s.net_id == nid})
    check("SIG routes at the board floor", widths('SIG') == [0.127], str(widths('SIG')))
    check("WIDE keeps its width", widths('WIDE') == [NET_W], str(widths('WIDE')))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print(f"FAILED {len(fails)}: " + '; '.join(fails))
    sys.exit(1)
print("ALL PASS")
