#!/usr/bin/env python3
"""route.py holds a via's hole to Board Setup's min_hole_clearance.

KiCad's board minimum `min_hole_clearance` is the first, condition-less rule
of its hole_clearance vector: it binds every hole, a via's included, unless a
`.kicad_dru` rule overrides it. route.py held it only on unplated holes and
kept copper off a via's hole through the copper clearance to the ring alone,
so with a thin ring (ring + clearance < min_hole_clearance) it routed copper
KiCad's DRC flags as hole_clearance.

Fixtures: tests/fixtures/1853 track_tight / track_fits (a wall with one slot,
a 0.6/0.3 mm via of another net standing in it), with the `.kicad_dru` rule
file emptied and the board's min_hole_clearance raised from 0.1 to 0.254 mm,
the value the 1853 rule file carries. The ring's keep-out is 0.3 + 0.0889 +
0.05 = 0.4389 mm from the via's centre, the floor's 0.15 + 0.254 + 0.05 =
0.454. track_tight leaves the row at 0.45 only; track_fits also the rows from
0.46.

Checks:
  1. the floor is installed as a hole rule when it binds (0.254 mm) and not
     when the ring already holds it (the fixture's own 0.1 mm);
  2. track_tight: SIG is not routed (unfixed: routed through the slot, 0.45
     mm from the via's centre);
  3. track_fits: SIG routes, its track stands 0.454 mm or more off the via's
     centre, check_drc is clean and the run says so.
"""
import json
import os
import sys
import tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(REPO, 'py_router'))
sys.path.insert(0, TESTS)
from test_1853_route_hole_clearance import (  # noqa: E402
    NO_RULES, FAILS, ok, stage, route, drc, load, rules_of, sig_reach)

FLOOR = 0.254


def stage_floor(td, name, floor):
    board = stage(td, name, dru=NO_RULES)
    pro = os.path.splitext(board)[0] + '.kicad_pro'
    with open(pro, encoding='utf-8') as fh:
        doc = json.load(fh)
    doc['board']['design_settings']['rules']['min_hole_clearance'] = floor
    with open(pro, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(doc, fh, indent=2)
    return board


def installed(td):
    print("1. the board floor as a hole rule")
    board = stage_floor(os.path.join(td, 'unit'), 'track_fits', FLOOR)
    pcb = load(board)
    hr = rules_of(board, pcb).hole_rules
    via = pcb.vias[0]
    ok(hr is not None and all(abs((hr.via_value(via, l) or 0) - FLOOR) < 1e-9
                              for l in pcb.board_info.copper_layers),
       f"min_hole_clearance {FLOOR} binds a via's hole on every layer")
    board = stage(os.path.join(td, 'unit_low'), 'track_fits', dru=NO_RULES)
    ok(rules_of(board, load(board)).hole_rules is None,
       "min_hole_clearance 0.1 (ring + clearance already hold it): nothing installed")


def tight(td):
    print("2. track_tight")
    sub = os.path.join(td, 'tight')
    out, routed, failed, _log = route(sub, stage_floor(sub, 'track_tight', FLOOR), 'out', True)
    reach = sig_reach(load(out), 15.0, 6.0) if routed else None
    ok(routed == [] and failed == ['SIG'],
       f"SIG is not routed past the via's hole: {routed} / {failed}"
       + (f" (its track stands {reach:.3f} mm from the via's centre)" if reach else ''))


def fits(td):
    print("3. track_fits")
    sub = os.path.join(td, 'fits')
    out, routed, failed, log = route(sub, stage_floor(sub, 'track_fits', FLOOR), 'out', True)
    ok(routed == ['SIG'] and failed == [], f"SIG routes: {routed} / {failed}")
    ok(f"Copper-to-hole {FLOOR:g}mm (the board's min_hole_clearance) also held at via" in log,
       "the run says the floor is held at via holes")
    if routed:
        d = sig_reach(load(out), 15.0, 6.0)
        need = 0.15 + FLOOR + 0.05
        ok(d >= need - 1e-6, f"its track stays {d:.3f} mm from the via's centre (needs {need:.3f})")
        ok('NO DRC VIOLATIONS FOUND' in drc(out), "check_drc is clean")


def main():
    with tempfile.TemporaryDirectory() as td:
        installed(td)
        tight(td)
        fits(td)
    if FAILS:
        print(f"\nFAILED ({len(FAILS)}):")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\nALL PASSED")
    return 0


if __name__ == '__main__':
    sys.exit(main())
