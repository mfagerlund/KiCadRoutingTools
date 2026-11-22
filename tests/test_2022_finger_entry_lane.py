#!/usr/bin/env python3
"""Each edge finger keeps an entry lane in front of its inner end (#2022 follow-up).

#2022 made the finger strip copper-free past the fingers' inner ends, so a
finger can be entered from its inner end only. Nothing kept that end free:
a neighbour's track or via parked right in front of it boxes the finger in,
and its net fails. `derive_finger_bands` now adds, per finger, an 'entry'
keep-out FINGER_ENTRY_DEPTH deep in front of the inner end, the finger's
width wide, on the finger's layer, refusing every other net's tracks and
vias. It is routing guidance, not a strip defect: check_drc does not grade it.

Board: test_2022_finger_band's plug (four fingers on F.Cu, inner ends at
y = 12.6) plus NX pads left and right of the plug just above the inner ends,
whose shortest path runs straight across the fingers' entries.

  1. derive: one entry lane per finger, FINGER_ENTRY_DEPTH deep, finger wide,
     binding every net but its finger's, no tracks, no vias;
  2. route.py: NX and SIG both route and connect; no NX copper in any entry
     lane;
  3. check_drc: a foreign track in an entry lane is no finger-band row.

    python3 tests/test_2022_finger_entry_lane.py
"""
import os
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)

import test_2022_finger_band as fb                               # noqa: E402
import run_utils                                                 # noqa: E402

failures = []
LANE_Y = fb.INNER - 0.5          # NX's straight line, inside the entry lanes


def ok(label, cond, detail=''):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not cond:
        failures.append(label)


def n3_pads():
    return fb._pad('U3', 2.0, LANE_Y, 'NX') + fb._pad('U4', 18.0, LANE_Y, 'NX')


def derive(tmp):
    print('1. derive: an entry lane per finger')
    import net_keepouts
    from net_keepouts import derive_finger_bands
    depth = getattr(net_keepouts, 'FINGER_ENTRY_DEPTH', None)
    kos = derive_finger_bands(fb.parse(fb.write_board(os.path.join(tmp, 'd.kicad_pcb'))))
    lanes = {k['pad']: k for k in kos if k.get('finger_band') == 'entry'}
    ok('four entry lanes', len(lanes) == 4, str(sorted(lanes)))
    good = depth is not None and len(lanes) == 4
    for p, k in sorted(lanes.items()):
        i = int(p) - 1
        x0, y0, x1, y1 = fb.bounds(k['polygon'])
        cx = fb.PLUG_AT[0] + fb.FINGER_X[i]
        good &= (abs(y1 - fb.INNER) < 1e-6 and abs(y0 - (fb.INNER - depth)) < 1e-6
                 and x0 <= cx - 0.75 + 1e-6 and x1 >= cx + 0.75 - 1e-6
                 and k['layers'] == {'F.Cu'}
                 and not k['tracks_allowed'] and not k['vias_allowed']
                 and fb.FINGER_NETS[i] not in k['nets']
                 and all(n in k['nets'] for n in fb.FINGER_NETS if n != fb.FINGER_NETS[i]))
    ok('each lane: depth in front of the inner end, finger wide, F.Cu, '
       'no tracks or vias of any other net', good)


def in_lanes(pcb, net, lanes):
    nid = {n.name: i for i, n in pcb.nets.items()}[net]
    hits = []
    for s in pcb.segments:
        if s.net_id != nid or s.layer != 'F.Cu':
            continue
        for k in lanes:
            x0, y0, x1, y1 = fb.bounds(k['polygon'])
            # sample the centreline; half width + nothing: a touch is a hit
            for t in [j / 20 for j in range(21)]:
                x = s.start_x + t * (s.end_x - s.start_x)
                y = s.start_y + t * (s.end_y - s.start_y)
                h = s.width / 2
                if x0 - h < x < x1 + h and y0 - h < y < y1 + h:
                    hits.append((k['name'], round(x, 2), round(y, 2)))
                    break
    for v in pcb.vias:
        if v.net_id != nid:
            continue
        for k in lanes:
            x0, y0, x1, y1 = fb.bounds(k['polygon'])
            h = v.size / 2
            if x0 - h < v.x < x1 + h and y0 - h < v.y < y1 + h:
                hits.append((k['name'], 'via'))
    return hits


def route(tmp):
    print('2. route.py keeps the entries free')
    from net_keepouts import derive_finger_bands
    src = fb.write_board(os.path.join(tmp, 'in.kicad_pcb'), extra=n3_pads())
    out = os.path.join(tmp, 'out.kicad_pcb')
    run_utils.run_step([run_utils.tool('route.py'), src, out, '--nets', 'NX', 'SIG',
                        '--layers', 'F.Cu', 'B.Cu', '--clearance', '0.2',
                        '--track-width', '0.25', '--via-size', '0.6', '--via-drill', '0.3'],
                       outputs=[out])
    r = subprocess.run([sys.executable, '-X', 'utf8', run_utils.tool('check_connected.py'),
                        out, '--nets', 'NX', 'SIG'], capture_output=True, text=True,
                       env=run_utils.tool_env(), timeout=600)
    ok('NX and SIG connect', r.returncode == 0 and 'ALL NETS FULLY CONNECTED' in r.stdout,
       r.stdout[-300:])
    pcb = fb.parse(out)
    # the lanes as this test defines them, independent of the build under test
    lanes = []
    for i, x in enumerate(fb.FINGER_X):
        cx = fb.PLUG_AT[0] + x
        lanes.append({'name': f'J1.{i + 1}', 'polygon': [
            (cx - 0.75, fb.INNER - 1.0), (cx + 0.75, fb.INNER - 1.0),
            (cx + 0.75, fb.INNER), (cx - 0.75, fb.INNER)]})
    hits = in_lanes(pcb, 'NX', lanes)
    ok('no NX copper in front of any finger', hits == [], str(hits[:6]))
    del derive_finger_bands


def grade(tmp):
    print('3. check_drc does not grade the entry lane')
    bad = fb._segment(5.0, LANE_Y, 15.0, LANE_Y, 'F.Cu', 'NX')
    path = fb.write_board(os.path.join(tmp, 'bad.kicad_pcb'), extra=bad)
    rows, _acc = fb.band_rows(path)
    ok('a foreign track across the entries: no finger-band row',
       not any(r.get('finger_band') == 'entry' for r in rows) and rows == [],
       str([(r['area'], r['item']) for r in rows]))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        derive(tmp)
        route(tmp)
        grade(tmp)
    print('\n' + ('ALL PASS' if not failures else f'{len(failures)} FAILED: {failures}'))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
