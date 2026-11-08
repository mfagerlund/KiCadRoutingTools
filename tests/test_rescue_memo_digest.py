"""The rescue memo's copper digest (2026-11-08): the same copper hashes the same, nets are named not numbered
(ids are re-assigned per parse), copper moved inside the rescue window changes it, copper moved far outside does
not, and a net with no copper has none."""
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))

from net_rescue import _rescue_digest  # noqa: E402


def board(ids=(1, 2), near_dx=0.0, far_dx=0.0):
    a, b = ids
    nets = {a: NS(name='SIG'), b: NS(name='GND')}
    pads = {a: [NS(net_id=a, component_ref='U1', pad_number='1', global_x=10.0, global_y=10.0),
                NS(net_id=a, component_ref='U2', pad_number='3', global_x=14.0, global_y=10.0)],
            b: [NS(net_id=b, component_ref='U3', pad_number='2', global_x=300.0, global_y=300.0)]}
    segs = [NS(net_id=b, layer='F.Cu', width=0.13, start_x=11.0 + near_dx, start_y=12.0,
               end_x=13.0 + near_dx, end_y=12.0),
            NS(net_id=b, layer='F.Cu', width=0.13, start_x=200.0 + far_dx, start_y=200.0,
               end_x=210.0 + far_dx, end_y=200.0)]
    vias = [NS(net_id=b, x=12.0, y=8.0, size=0.45, drill=0.2, layers=['F.Cu', 'B.Cu'])]
    return NS(nets=nets, pads_by_net=pads, segments=segs, vias=vias, zones=[])


def test_digest():
    d0 = _rescue_digest(board(), 1)
    assert d0 is not None
    assert _rescue_digest(board(), 1) == d0
    assert _rescue_digest(board(ids=(7, 9)), 7) == d0, 'net ids differ between parses; names do not'
    assert _rescue_digest(board(near_dx=0.5), 1) != d0, 'copper moved inside the window'
    assert _rescue_digest(board(far_dx=5.0), 1) == d0, 'copper moved far outside the window'
    empty = board()
    empty.pads_by_net[1] = []
    assert _rescue_digest(empty, 1) is None


if __name__ == '__main__':
    test_digest()
    print('PASS')
