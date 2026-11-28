#!/usr/bin/env python3
"""Same-net vias from two steps must still meet hole-to-hole.

Same-net copper is no obstacle, so the via maps never refuse a via next to a
same-net via laid by an EARLIER step (plane finalize, rescue, stitching,
repair_planes). The only guard, `merge_close_same_net_vias`, ran over one
step's new vias. KiCad's hole_to_hole is net-independent: ecp5-dc-scm shipped
two GND pairs 0.2 mm apart this way (DRC added 2).

`via_hole_guard` grades the WRITTEN board of every via-placing step against
the step's resolved hole-to-hole and removes the later via of each same-net
pair, re-anchoring its tracks on the kept hole.

Run: python3 -X utf8 tests/test_tapfloor_same_net_via_holes.py
"""
import math
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_utils  # noqa: E402

ROOT = run_utils.ROOT_DIR
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

H2H = 0.25

BOARD = """(kicad_pcb
\t(version 20241229)
\t(generator "synth")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(25 "Edge.Cuts" user)
\t)
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "SIG")
\t(gr_rect (start 0 0) (end 50 50) (layer "Edge.Cuts") (uuid "edge"))
\t(segment (start 10 20) (end 20 20) (width 0.2) (layer "F.Cu") (net 1) (uuid "s1"))
\t(via (at 20 20) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
\t(via (at 20.4 20) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "v2"))
\t(segment (start 20.4 20) (end 30 20) (width 0.25) (layer "B.Cu") (net 1) (uuid "s2"))
\t(via (at 21 25) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "v3"))
\t(via (at 21 25.7) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "v4"))
)
"""


def _vias(text):
    return [(float(m.group(1)), float(m.group(2)))
            for m in re.finditer(r'\(via \(at ([-\d.]+) ([-\d.]+)\)', text)]


def _write(text):
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb')
    with os.fdopen(fd, 'w', encoding='utf-8', newline='') as fh:
        fh.write(text)
    return path


def test_guard_removes_later_same_net_via_and_keeps_connectivity():
    from via_hole_guard import guard_step_output
    path = _write(BOARD)
    try:
        r = guard_step_output(path, H2H, label='test')
        assert r and r['removed'] == 1, r
        text = open(path, encoding='utf-8').read()
        vias = _vias(text)
        # v2 (later, 0.4 mm centre gap < 0.3 + 0.25) goes; v1 stays.
        assert (20.0, 20.0) in vias and (20.4, 20.0) not in vias, vias
        # Different-net pair v3/v4 is untouched (the router's clearance job).
        assert (21.0, 25.0) in vias and (21.0, 25.7) in vias, vias
        # Every remaining same-net hole pair meets hole-to-hole.
        assert all(math.hypot(a[0] - b[0], a[1] - b[1]) >= 0.3 + H2H - 1e-6
                   for i, a in enumerate(vias[:2]) for b in vias[i + 1:2]), vias
        # s2's end at the removed via is re-anchored to v1 on its own layer.
        assert re.search(r'\(segment \(start 20\.4 20\) \(end 20 20\) \(width 0\.25\) '
                         r'\(layer "B\.Cu"\) \(net 1\)', text), text
        # Idempotent and deterministic: a second pass changes nothing.
        before = text
        r2 = guard_step_output(path, H2H, label='test')
        assert r2['removed'] == 0
        assert open(path, encoding='utf-8').read() == before
    finally:
        os.remove(path)


def test_guard_keeps_pair_when_floor_met():
    from via_hole_guard import guard_step_output
    path = _write(BOARD)
    try:
        r = guard_step_output(path, 0.05, label='test')
        assert r['removed'] == 0, r
    finally:
        os.remove(path)


def test_every_via_placing_cli_runs_the_guard_on_its_output():
    for name in ('route.py', 'route_diff.py', 'route_planes.py', 'repair_planes.py'):
        src = open(os.path.join(ROOT, 'py_router', name), encoding='utf-8').read()
        assert re.search(r'guard_step_output\(args\.output_file, '
                         r'args\.hole_to_hole_clearance\)', src), name


if __name__ == '__main__':
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith('test_') and callable(f):
            try:
                f()
                print(f'PASS {n}')
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f'FAIL {n}: {e!r}')
    sys.exit(1 if fails else 0)
