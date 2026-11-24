#!/usr/bin/env python3
"""Fine escape: a pad the run's own grid escapes gets no stub.

The geometric test of the pre-pass (straight exit band, conservative pad
raster) flags more pads than the main pass fails on: the main pass has more
ways out of a pad than a straight exit. A stub on such a pad only pins the
exit point and costs the main pass search, so the pre-pass first tries the
stub at the run's grid in the same window and leaves the pad alone when that
routes.

Fixture: the two-column part of test_2030_fine_escape (0.5 mm pitch,
1.2 x 0.3 mm pads, pad centres on y = 0.05 mod 0.1) at 0.2 mm track / 0.1 mm
clearance. The geometric test flags the 8 inner pads, yet the 0.1 mm main
pass routes all 12 nets on its first attempt with the pre-pass off.

Checks:
  1. the geometric test flags 8 pads;
  2. with the pre-pass off the main pass fails none: the pads are not boxed;
  3. the pre-pass reports 0 grid-boxed pads and places 0 stubs (8 before the
     probe);
  4. at 0.25 mm track / 0.2 mm clearance, where the main pass does fail the 8
     nets, the pre-pass still places its 8 stubs.
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
import test_2030_fine_escape as fx  # noqa: E402


def _config(track, clearance):
    from routing_config import GridRouteConfig
    return GridRouteConfig(track_width=track, clearance=clearance,
                           via_size=0.8, via_drill=0.4, grid_step=0.1,
                           layers=['F.Cu', 'B.Cu'])


def _prepass(d, track, clearance):
    from kicad_parser import parse_kicad_pcb
    from fine_escape import find_grid_boxed_pads, fine_escape_prepass
    pcb = parse_kicad_pcb(fx._write(d, f'p_{track}_{clearance}.kicad_pcb', 0.0))
    ids = [nid for nid, n in pcb.nets.items() if n.name.startswith('S')]
    cfg = _config(track, clearance)
    flagged = len(find_grid_boxed_pads(pcb, cfg, ids))
    with contextlib.redirect_stdout(io.StringIO()):
        boxed, placed = fine_escape_prepass(pcb, cfg, ids, [])
    return flagged, boxed, placed


def _main_pass_failures(d):
    import env_knobs
    import route
    src = fx._write(d, 'route.kicad_pcb', 0.0)
    out = os.path.join(d, 'out.kicad_pcb')
    names = [f'S{i}' for i in range(1, 2 * fx.ROWS + 1)]
    saved = env_knobs.FINE_ESCAPE
    buf = io.StringIO()
    try:
        env_knobs.FINE_ESCAPE = False
        with contextlib.redirect_stdout(buf):
            route.batch_route(src, out, names, layers=['F.Cu', 'B.Cu'],
                              track_width=0.2, clearance=0.1, via_size=0.8,
                              via_drill=0.4, grid_step=0.1)
    finally:
        env_knobs.FINE_ESCAPE = saved
    return (buf.getvalue().count('FAILED: Could not find route'),
            fx._connected(fx.evidence(out)), len(names))


def main():
    d = tempfile.mkdtemp(prefix='t2030p_')
    errs = []
    try:
        flagged, boxed, placed = _prepass(d, 0.2, 0.1)
        print(f"  0.2/0.1: geometric test flags {flagged}, pre-pass reports "
              f"{boxed} grid-boxed, {placed} stub(s)")
        if flagged != 8:
            errs.append(f"geometric test flags {flagged}, want 8 "
                        "(fixture no longer shows the case)")
        failed, connected, total = _main_pass_failures(d)
        print(f"  0.2/0.1, pre-pass off: main pass failed {failed}, "
              f"connected {connected}/{total}")
        if failed or connected != total:
            errs.append(f"main pass failed {failed}, connected "
                        f"{connected}/{total} without the pre-pass, want 0 "
                        "failed (fixture is boxed after all)")
        if (boxed, placed) != (0, 0):
            errs.append(f"pre-pass: {boxed} grid-boxed, {placed} stub(s) on "
                        "pads the run grid escapes, want 0 and 0")
        flagged, boxed, placed = _prepass(d, 0.25, 0.2)
        print(f"  0.25/0.2: geometric test flags {flagged}, pre-pass reports "
              f"{boxed} grid-boxed, {placed} stub(s)")
        if (boxed, placed) != (8, 8):
            errs.append(f"pre-pass at 0.25/0.2: {boxed} grid-boxed, {placed} "
                        "stub(s), want 8 and 8")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    for e in errs:
        print(f"FAIL: {e}")
    print("PASS" if not errs else f"{len(errs)} failure(s)")
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
