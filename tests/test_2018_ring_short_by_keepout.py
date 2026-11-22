#!/usr/bin/env python3
"""Under ``--escalation board`` a ring-short via may still ship where its
KEEP-OUT holds the board's hole clearance (#2018 follow-up).

#2018 made the ring REQUIRED under ``board`` by offering only each rung
raised to hold it (drill + 2*(hole_clearance - clearance)). Its premise --
"a ring-short via is clean only where its raised twin is" -- holds at the
clearance the ring is priced at. On a pair at a HIGHER clearance (a net
class above the board default) the raised twin's copper needs more room than
the short via held at the hole clearance, so the raised-only ladder loses
legal sites: at clearance 0.1 / class 0.15 / hole clearance 0.25, a 0.45/0.2
via needs its centre max(0.225 + 0.15, 0.1 + 0.25) = 0.375 off the class
net's copper, its raised 0.5/0.2 twin 0.40.

The fix holds every new via's hole at the board's hole clearance by its
keep-out (`GridRouteConfig.via_hole_extra`, the #189 refit) under ``board``,
and offers the rung as it is after its raised twin at the sites that check
it so (the rung-1 small via pair, the refit).

Checks (clearance 0.1, hole clearance 0.25, standard tier, 4 layers):
  1. board: the keep-out-checked ladder is 0.5/0.2 then 0.45/0.2; the plain
     ladder is still 0.5/0.2 only (test_2018);
  2. via_hole_extra under board: a 0.45/0.2 via's hole binds off a track at
     0.1 (+0.025) and off another 0.45/0.2 via, not at 0.15, not for the
     ring-holding 0.5/0.2, and never under fab;
  3. refit under board, a class-0.15 track: the 0.5 grazes, the 0.45/0.2
     fits (its hole measured raw, not class-folded) and is returned;
  4. refit under board, a track the 0.45/0.2 clears by copper but not by
     hole: nothing fits (no ring-short via inside the hole clearance);
  5. refit under fab at the same site: the 0.45/0.2 ships (fab unchanged).

    python3 tests/test_2018_ring_short_by_keepout.py
"""
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

import fab_tiers  # noqa: E402
from kicad_parser import BoardInfo, Net  # noqa: E402
from routing_config import GridRouteConfig  # noqa: E402
from synth import make_seg, make_via, make_pcb  # noqa: E402

OWN, FOREIGN = 1, 99
LAYERS = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']
fails = []


def ok(name, cond, detail=''):
    print(('PASS: ' if cond else 'FAIL: ') + name + (f'  {detail}' if detail else ''))
    if not cond:
        fails.append(name)


def _pairs(rungs):
    return [(round(r['via_diameter'], 3), round(r['via_drill'], 3)) for r in rungs]


def cfg(size=0.5, drill=0.2, foreign_class=None):
    c = GridRouteConfig(clearance=0.1, track_width=0.1, via_size=size,
                        via_drill=drill, layers=list(LAYERS), grid_step=0.05)
    c.hole_to_hole_clearance = 0.25
    if foreign_class is not None:
        c.set_net_clearances({FOREIGN: foreign_class}, routed_net_ids=[OWN])
    return c


def board(track_y):
    bi = BoardInfo(layers={}, board_bounds=None, copper_layers=list(LAYERS))
    return make_pcb(segments=[make_seg(-2, track_y, 2, track_y, net_id=FOREIGN,
                                       width=0.2)],
                    board_info=bi,
                    nets={OWN: Net(OWN, '/OWN'), FOREIGN: Net(FOREIGN, '/F')})


def policy(esc):
    fab_tiers.set_default_fab_tier('standard')
    fab_tiers.set_escalation_policy(esc, {})
    fab_tiers.set_via_hole_clearance(0.25, 0.1)


def main():
    from single_ended_routing import _unblock_via_refit
    saved_tier, saved_esc = fab_tiers._DEFAULT_TIER, fab_tiers._ESCALATION
    try:
        policy('board')
        plain = _pairs(fab_tiers.escalation_rungs(4))
        try:
            kept = _pairs(fab_tiers.escalation_rungs(4, hole_ring_by_keepout=True))
        except TypeError as e:
            kept = repr(e)
        ok('1. board: keep-out ladder 0.5/0.2, 0.45/0.2; plain ladder 0.5/0.2',
           kept == [(0.5, 0.2), (0.45, 0.2)] and plain == [(0.5, 0.2)],
           f'keep-out {kept} plain {plain}')

        short = cfg(0.45, 0.2)
        other = make_via(0, 1, net_id=FOREIGN, size=0.45, drill=0.2)
        got = (round(short.via_hole_extra(0.1), 4),
               round(short.via_hole_extra(0.15), 4),
               round(short.via_hole_extra(0.1, other), 4),
               round(cfg().via_hole_extra(0.1), 4))
        policy('fab')
        got_fab = round(short.via_hole_extra(0.1), 4)
        ok('2. via_hole_extra: board 0.025 at 0.1, 0 at 0.15, 0.025 off a short '
           'via, 0 for 0.5/0.2; fab 0',
           got == (0.025, 0.0, 0.025, 0.0) and got_fab == 0.0,
           f'board {got} fab {got_fab}')

        policy('board')
        # The refit allows UNBLOCK_REFIT_MARGIN_MM (0.05) of slack: it picks a
        # size, the site's legality comes from the via map's keep-out.
        # Track copper edge 0.34 off the via centre: the class-0.15 pair
        # needs 0.40 (0.5) or 0.375 (0.45) less 0.05; the 0.45's hole 0.35.
        r3 = _unblock_via_refit(board(0.44), OWN, 0.0, 0.0, (0.5, 0.2),
                                cfg(foreign_class=0.15))
        ok('3. board refit, class 0.15: 0.45/0.2 fits where 0.5/0.2 grazes',
           r3 == (0.45, 0.2), r3)
        # copper edge 0.29: the 0.45's copper clears (0.325 - 0.05), its
        # hole does not (0.35 - 0.05); the 0.5 needs 0.35 - 0.05
        r4 = _unblock_via_refit(board(0.39), OWN, 0.0, 0.0, (0.5, 0.2), cfg())
        ok('4. board refit: no ring-short via inside the hole clearance',
           r4 is None, r4)
        policy('fab')
        r5 = _unblock_via_refit(board(0.39), OWN, 0.0, 0.0, (0.5, 0.2), cfg())
        ok('5. fab refit: the 0.45/0.2 ships (completion first)',
           r5 == (0.45, 0.2), r5)
    finally:
        fab_tiers.set_via_hole_clearance(None, None)
        fab_tiers.set_escalation_policy(saved_esc, {})
        fab_tiers.set_default_fab_tier(saved_tier)
    print('\n' + ('ALL PASS' if not fails else f'{len(fails)} FAILED: {fails}'))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
