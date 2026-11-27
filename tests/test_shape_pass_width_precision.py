#!/usr/bin/env python3
"""The shape passes rebuild copper at the width it was routed at, to the nm.

``merge_collinear_segments`` and ``smooth_octolinear_chains`` group a net's
segments by width and rebuild each group's chain. The group key was
``round(width, 4)`` and the rebuilt segments took the KEY as their width, so a
track routed at a board minimum of 0.19812 mm (7.8 mil) shipped at 0.1981 mm,
2 nm under the minimum KiCad grades it at: a track_width violation per
rebuilt segment.

    python3 tests/test_shape_pass_width_precision.py
"""
import contextlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from kicad_parser import BoardInfo, Net, Pad, PCBData, Segment  # noqa: E402
from pcb_modification import (merge_collinear_segments,  # noqa: E402
                              smooth_octolinear_chains)
from routing_config import GridRouteConfig  # noqa: E402

W = 0.19812          # a board minimum with five decimals (7.8 mil)
CLR = 0.1


def _pad(ref, x, y):
    return Pad(ref, '1', x, y, 0.0, 0.0, 0.5, 0.5, 'circle', ['F.Cu'],
               1, '/SIG', pad_type='smd')


def _board(path):
    segs = [Segment(a[0], a[1], b[0], b[1], W, 'F.Cu', 1)
            for a, b in zip(path, path[1:])]
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'}, copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=(0.0, 0.0, 30.0, 30.0))
    return PCBData(board_info=bi, nets={1: Net(1, '/SIG')}, footprints={},
                   vias=[], segments=segs,
                   pads_by_net={1: [_pad('U1', *path[0]), _pad('U2', *path[-1])]})


def test_merge_keeps_the_routed_width():
    pcb = _board(((5.0, 10.0), (8.0, 10.0), (8.1, 10.0), (15.0, 10.0)))
    with contextlib.redirect_stdout(io.StringIO()):
        merge_collinear_segments([], pcb, None)
    assert len(pcb.segments) == 1, pcb.segments
    assert pcb.segments[0].width == W, pcb.segments[0].width


def test_smoothing_keeps_the_routed_width():
    pcb = _board(((10.0, 10.0), (10.0, 16.0), (16.0, 16.0)))
    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], track_width=W, clearance=CLR)
    with contextlib.redirect_stdout(io.StringIO()):
        _n, nets, _s, _a, _st = smooth_octolinear_chains([], pcb, clearance=CLR,
                                                         config=cfg)
    assert nets == 1, 'fixture: the L must be smoothed'
    widths = {s.width for s in pcb.segments}
    assert widths == {W}, widths


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'PASS {name}')
            except AssertionError as e:
                fails += 1
                print(f'FAIL {name}: {e}')
    sys.exit(1 if fails else 0)
