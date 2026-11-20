#!/usr/bin/env python3
"""Plane-pad tap that rips one pair leg off a shared connector rips its
partner too (repair_planes._tap_pad_with_ripup, _pair_partner_on_footprint).

The field case: tiliqua-motherboard J10 (HDMI, 0.5 mm pitch) has D0- on pad 9,
GND on pad 8, D0+ on pad 7. D0-'s coupling jog takes GND pad 8's lane; the
phase-2 tap ripped +5V, EX2_9 and /gpdi/CONN_D0+, landed its via in D0+'s
lane, and D0+'s reconnect was then boxed by D0-'s copper: 99.8%, D0+ open.
Ripping the partner with the leg lets both legs reroute around the tap; when
the leg cannot be restored, the partner is not restored either.

Rows:
  1. the tap rips D0+; its copper collides with the tap and stays ripped;
     D0- is ripped with it and stays ripped too (both in ripped_net_ids, no
     D0- copper left on the board).
  2. KICAD_TAP_PAIR_RIP=0: D0- is untouched.
  3. the leg's copper does not collide with the tap: the leg is restored,
     and so is its partner.
  4. partner without a pad on the tap pad's footprint: not ripped.
  5. a blocker that is no pair leg: nothing extra is ripped.

Run:  python3 tests/test_tap_pair_partner_rip.py
"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))

from kicad_parser import BoardInfo, Net, PCBData, Pad, Segment   # noqa: E402
from routing_config import GridRouteConfig                       # noqa: E402
import repair_planes as rdp                                      # noqa: E402

GND, DP, DM, SIG = 2, 5, 6, 7
FAILURES = []


def _check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' -- ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def _pad(ref, num, x, y, net_id, name):
    return Pad(component_ref=ref, pad_number=num, global_x=x, global_y=y,
               local_x=0.0, local_y=0.0, size_x=0.6, size_y=0.25, shape="rect",
               layers=["F.Cu"], net_id=net_id, net_name=name, pad_type="smd")


def _board(dm_ref="J10"):
    """J10 column at x 8: D0- 42.2, GND 42.7, D0+ 43.2; each leg runs east
    from its J10 pad to U1. A stranger SIG runs below."""
    bi = BoardInfo(layers={0: "F.Cu", 31: "B.Cu"}, copper_layers=["F.Cu", "B.Cu"])
    bi.board_bounds = (0.0, 0.0, 40.0, 60.0)
    dm_y = 42.2 if dm_ref == "J10" else 30.0
    segs = [Segment(start_x=8.0, start_y=43.2, end_x=14.0, end_y=43.2,
                    width=0.15, layer="F.Cu", net_id=DP),
            Segment(start_x=8.0, start_y=dm_y, end_x=14.0, end_y=dm_y,
                    width=0.15, layer="F.Cu", net_id=DM),
            Segment(start_x=8.0, start_y=50.0, end_x=14.0, end_y=50.0,
                    width=0.15, layer="F.Cu", net_id=SIG)]
    pads = {
        DP: [_pad("J10", "7", 8.0, 43.2, DP, "/gpdi/CONN_D0+"),
             _pad("U1", "1", 14.0, 43.2, DP, "/gpdi/CONN_D0+")],
        DM: [_pad(dm_ref, "9", 8.0, dm_y, DM, "/gpdi/CONN_D0-"),
             _pad("U1", "2", 14.0, dm_y, DM, "/gpdi/CONN_D0-")],
        SIG: [_pad("J10", "1", 8.0, 50.0, SIG, "/SIG"),
              _pad("U1", "3", 14.0, 50.0, SIG, "/SIG")],
        GND: [_pad("J10", "8", 8.0, 42.7, GND, "GND")],
    }
    nets = {GND: Net(GND, "GND"), DP: Net(DP, "/gpdi/CONN_D0+"),
            DM: Net(DM, "/gpdi/CONN_D0-"), SIG: Net(SIG, "/SIG")}
    return PCBData(board_info=bi, nets=nets, footprints={}, vias=[],
                   segments=segs, pads_by_net=pads)


def _run(pcb, blocker, tap_seg):
    """One tap with a stubbed router: the blocker is named by the consensus
    finder, the tap succeeds with tap_seg as its copper."""
    cfg = GridRouteConfig(track_width=0.15, clearance=0.1, via_size=0.45,
                          via_drill=0.2, grid_step=0.05)

    def fake_tap(*a, **k):
        return SimpleNamespace(success=True, segments=[tap_seg], via=None,
                               clearance_used=0.1, blocked_cells=[])

    orig_tap = rdp.tap_pad_with_escalation
    orig_blk = rdp._via_site_consensus_blocker
    rdp.tap_pad_with_escalation = fake_tap
    rdp._via_site_consensus_blocker = lambda *a, **k: blocker
    ripped, partial = [], []
    try:
        res = rdp._tap_pad_with_ripup(
            pad=pcb.pads_by_net[GND][0], pad_layer="F.Cu", net_id=GND,
            pcb_data=pcb, tap_config=cfg, blocker_config=cfg,
            max_search_radius=2.0, via_size=0.45, via_drill=0.2,
            max_rip_nets=1, protected_net_ids={GND},
            first_failure=SimpleNamespace(blocked_cells=[]),
            ripped_net_ids=ripped, verbose=False, partial_restores=partial)
    finally:
        rdp.tap_pad_with_escalation = orig_tap
        rdp._via_site_consensus_blocker = orig_blk
    return res, ripped, partial


def _tap(x1, y1, x2, y2):
    """Tap copper as the tap router returns it (segment dicts)."""
    return {'start': (x1, y1), 'end': (x2, y2), 'width': 0.15,
            'layer': 'F.Cu', 'net_id': GND}


def _over_dp():
    # GND tap copper laid along D0+'s lane: D0+ cannot be restored.
    return _tap(8.0, 43.2, 14.0, 43.2)


def _clear_of_all():
    return _tap(2.0, 42.7, 8.0, 42.7)


def _has(pcb, nid):
    return any(s.net_id == nid for s in pcb.segments)


def main():
    print("tap pair-partner rip:")
    os.environ.pop('KICAD_TAP_PAIR_RIP', None)

    pcb = _board()
    res, ripped, partial = _run(pcb, DP, _over_dp())
    _check("row 1: tap succeeds", res is not None and res.success)
    _check("row 1: D0+ stays ripped", DP in ripped, f"ripped {ripped}")
    _check("row 1: D0- ripped with it", DM in ripped, f"ripped {ripped}")
    _check("row 1: no D0- copper left", not _has(pcb, DM))
    _check("row 1: stranger untouched", _has(pcb, SIG) and SIG not in ripped)

    os.environ['KICAD_TAP_PAIR_RIP'] = '0'
    try:
        pcb = _board()
        res, ripped, partial = _run(pcb, DP, _over_dp())
        _check("row 2: knob off, D0- untouched",
               DM not in ripped and _has(pcb, DM), f"ripped {ripped}")
    finally:
        os.environ.pop('KICAD_TAP_PAIR_RIP', None)

    pcb = _board()
    res, ripped, partial = _run(pcb, DP, _clear_of_all())
    _check("row 3: clear tap restores both legs",
           not ripped and _has(pcb, DP) and _has(pcb, DM),
           f"ripped {ripped} partial {[p[0] for p in partial]}")

    pcb = _board(dm_ref="J11")
    res, ripped, partial = _run(pcb, DP, _over_dp())
    _check("row 4: partner off the footprint not ripped",
           DM not in ripped and _has(pcb, DM), f"ripped {ripped}")

    pcb = _board()
    res, ripped, partial = _run(pcb, SIG, _tap(8.0, 50.0, 14.0, 50.0))
    _check("row 5: non-pair blocker rips nothing else",
           ripped == [SIG] and _has(pcb, DP) and _has(pcb, DM),
           f"ripped {ripped}")

    print('FAILED: %d' % len(FAILURES) if FAILURES else 'ALL PASS')
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
