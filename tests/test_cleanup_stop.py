#!/usr/bin/env python3
"""Fork change 12: a stop request (route-board's --budget-s stop file, through batch_route's cancel_check) reaches
the cleanup pipeline. run_post_route_cleanup(cancel_check=...) skips the cosmetic passes once the stop is requested
and still runs the ledger-keeping ones (the this-run dead spur is swept); smooth_octolinear_chains(cancel_check=...)
stops between nets and leaves the copper as it stands. Without a stop request nothing changes.

    python tests/test_cleanup_stop.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))

from kicad_parser import PCBData, BoardInfo, Segment, Pad
from routing_config import GridRouteConfig
from pcb_modification import smooth_octolinear_chains
from cleanup_pipeline import run_post_route_cleanup

NET = 5


def _seg(x1, y1, x2, y2, layer='F.Cu', net=NET, width=0.2):
    return Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2, width=width, layer=layer, net_id=net)


def _pad(ref, x, y, net=NET):
    return Pad(component_ref=ref, pad_number='1', global_x=x, global_y=y, local_x=0, local_y=0, size_x=0.5,
               size_y=0.5, shape='circle', layers=['F.Cu'], net_id=net, net_name='N')


def make_board():
    """pad A -- staircase (this run) -- pad B, plus a this-run dead spur off pad B."""
    pa, pb = _pad('A', 0, 0), _pad('B', 6, 3)
    stair = [_seg(0, 0, 2, 0), _seg(2, 0, 2, 1), _seg(2, 1, 4, 1), _seg(4, 1, 4, 2), _seg(4, 2, 6, 2),
             _seg(6, 2, 6, 3)]
    spur = _seg(6, 3, 6, 6)
    pcb = PCBData(board_info=BoardInfo(layers={}, copper_layers=['F.Cu', 'B.Cu'], board_bounds=None),
                  nets={}, footprints={}, vias=[], segments=[*stair, spur], pads_by_net={NET: [pa, pb]})
    results = [{'net_id': NET, 'new_segments': [*stair, spur], 'new_vias': []}]
    return pcb, results, stair, spur


def run():
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        passed += bool(cond)
        failed += not cond
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")

    cfg = GridRouteConfig(grid_step=0.1, clearance=0.15, via_size=0.6, track_width=0.2, via_drill=0.3,
                          layers=['F.Cu', 'B.Cu'])

    # smoothing alone: a stop request leaves the staircase untouched and says so
    pcb, results, stair, spur = make_board()
    n, nets, strip, added, stats = smooth_octolinear_chains(results, pcb, {NET}, clearance=cfg.clearance,
                                                            config=cfg, cancel_check=lambda: True)
    check("smoothing: stop requested -> no net touched", n == 0 and nets == 0 and stats.get('stopped') is True)
    check("smoothing: copper as it stood", len(pcb.segments) == 7 and all(s in pcb.segments for s in stair))

    pcb, results, stair, spur = make_board()
    n, nets, strip, added, stats = smooth_octolinear_chains(results, pcb, {NET}, clearance=cfg.clearance,
                                                            config=cfg, cancel_check=lambda: False)
    check("smoothing: no stop -> the staircase collapses", n > 0 and stats['spans'] > 0 and not stats.get('stopped'))

    # the pipeline: stopped = cosmetic passes skipped, ledger-keeping passes still run (the spur is swept)
    pcb, results, stair, spur = make_board()
    out = run_post_route_cleanup(results, pcb, {NET}, cfg, smooth=True, cancel_check=lambda: True)
    check("pipeline: stop recorded", out.counts.get('stopped') == 1)
    check("pipeline: cosmetic passes skipped", out.counts.get('graze_pruned', 0) == 0
          and out.counts.get('microshifted', 0) == 0 and out.counts.get('smoothed_spans', 0) == 0)
    check("pipeline: this-run dead spur still swept", spur not in pcb.segments
          and spur not in results[0]['new_segments'])
    check("pipeline: the staircase kept (no smoothing)", all(s in pcb.segments for s in stair))

    pcb, results, stair, spur = make_board()
    out = run_post_route_cleanup(results, pcb, {NET}, cfg, smooth=True, cancel_check=lambda: False)
    check("pipeline: no stop -> smoothing ran", 'stopped' not in out.counts and out.counts.get('smoothed_spans', 0) > 0)
    check("pipeline: no stop -> spur swept too", spur not in pcb.segments)

    pcb, results, stair, spur = make_board()
    out = run_post_route_cleanup(results, pcb, {NET}, cfg, smooth=True)
    check("pipeline: default (no cancel_check) unchanged", 'stopped' not in out.counts
          and out.counts.get('smoothed_spans', 0) > 0)

    print(f"{passed} passed, {failed} failed")
    return failed == 0


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
