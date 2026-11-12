"""The rescue memo's copper digest (2026-11-08): the same copper hashes the same, nets are named not numbered
(ids are re-assigned per parse), copper moved inside the rescue window changes it, copper moved far outside does
not, and a net with no copper has none. A rescue also depends on pad sizes, pad clearances and keep-out rule areas:
changing any of them must change the digest. The memo is scoped per board: another board with a net of the same
name never shares an entry."""
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))

from net_rescue import _rescue_digest, _board_scope  # noqa: E402


def _pad(nid, ref, num, x, y, **kw):
    p = dict(net_id=nid, component_ref=ref, pad_number=num, global_x=x, global_y=y, size_x=0.6, size_y=0.6,
             shape='rect', rotation=0.0, rect_rotation=0.0, roundrect_rratio=0.0, layers=['F.Cu'], drill=0.0,
             pad_type='smd', local_clearance=0.0)
    p.update(kw)
    return NS(**p)


def board(ids=(1, 2), near_dx=0.0, far_dx=0.0, pad_kw=None, keepouts=None, refs=('U1', 'U2', 'U3')):
    a, b = ids
    nets = {a: NS(name='SIG'), b: NS(name='GND')}
    pads = {a: [_pad(a, 'U1', '1', 10.0, 10.0, **(pad_kw or {})),
                _pad(a, 'U2', '3', 14.0, 10.0)],
            b: [_pad(b, 'U3', '2', 300.0, 300.0)]}
    segs = [NS(net_id=b, layer='F.Cu', width=0.13, start_x=11.0 + near_dx, start_y=12.0,
               end_x=13.0 + near_dx, end_y=12.0),
            NS(net_id=b, layer='F.Cu', width=0.13, start_x=200.0 + far_dx, start_y=200.0,
               end_x=210.0 + far_dx, end_y=200.0)]
    vias = [NS(net_id=b, x=12.0, y=8.0, size=0.45, drill=0.2, layers=['F.Cu', 'B.Cu'])]
    bi = NS(keepouts=list(keepouts or []), board_outline=[(0, 0), (400, 0), (400, 400), (0, 400)])
    return NS(nets=nets, pads_by_net=pads, segments=segs, vias=vias, zones=[], board_info=bi,
              footprints={r: None for r in refs})


KEEPOUT = {'polygon': [(9, 9), (11, 9), (11, 11), (9, 11)], 'holes': [], 'layers': {'F.Cu'},
           'tracks_allowed': False, 'vias_allowed': False, 'copper_pour_allowed': True,
           'pads_allowed': True, 'footprints_allowed': True, 'owner': None, 'name': ''}


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


def test_digest_follows_pads_and_rule_areas():
    d0 = _rescue_digest(board(), 1)
    assert _rescue_digest(board(pad_kw={'size_x': 1.0}), 1) != d0, 'pad resized'
    assert _rescue_digest(board(pad_kw={'shape': 'roundrect', 'roundrect_rratio': 0.25}), 1) != d0, 'pad shape'
    assert _rescue_digest(board(pad_kw={'rotation': 45.0}), 1) != d0, 'pad rotated'
    assert _rescue_digest(board(pad_kw={'local_clearance': 0.3}), 1) != d0, 'pad clearance override'
    assert _rescue_digest(board(keepouts=[KEEPOUT]), 1) != d0, 'keep-out rule area added'
    ko2 = dict(KEEPOUT, vias_allowed=True)
    assert _rescue_digest(board(keepouts=[ko2]), 1) != _rescue_digest(board(keepouts=[KEEPOUT]), 1), \
        'keep-out rule changed'


def test_memo_scoped_per_board():
    assert _board_scope(board()) == _board_scope(board(ids=(7, 9))), 're-parse of the same board'
    assert _board_scope(board()) != _board_scope(board(refs=('U1', 'U2', 'U4'))), 'another board'


if __name__ == '__main__':
    test_digest()
    test_digest_follows_pads_and_rule_areas()
    test_memo_scoped_per_board()
    print('PASS')
