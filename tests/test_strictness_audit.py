#!/usr/bin/env python3
"""
Strictness audit (2026-11-27): the start-of-route guard and the reference envelope.

1. strictness_guard: a limit fires only when the board's rule allows less than the router uses; strict mode
   lists the limits with no reason on record; every entry carries id, what and site.
2. reference_envelope.compare: a human layout that uses a smaller via, a narrower track, a via in a pad or a
   blind via than ours yields the matching flag; equal layouts yield none.

Run:
    python3 tests/test_strictness_audit.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'py_router'))

import strictness_guard as g  # noqa: E402
import reference_envelope as env  # noqa: E402


def _ctx(**kw):
    c = dict(board_min={'min_track_width': 0.1, 'min_via_diameter': 0.45, 'min_copper_edge_clearance': 0.3,
                        'min_via_annular_width': 0.2},
             default_width=0.25, default_clearance=0.2, class_widths=[0.25], class_clearances=[0.2],
             pair_gaps=[], grid=0.1, via_size=0.6, via_drill=0.3,
             fab={'track_width': 0.127, 'board_edge': 0.2, 'via_diameter': 0.45, 'annular': 0.2},
             copper_count=2, allow_blind=False, allow_micro=False, via_span=None)
    c.update(kw)
    return c


def _ids(ctx):
    return {l['id'] for l, _ in g.evaluate(ctx)}


def test_guard_width_fires_only_below_class_width():
    assert {'W1', 'W2', 'W3', 'W4'} <= _ids(_ctx())
    tight = _ctx(board_min={'min_track_width': 0.25, 'min_via_diameter': 0.5, 'min_copper_edge_clearance': 0.3})
    assert not {'W1', 'W2', 'W3', 'W4', 'W5', 'W6'} & _ids(tight)


def test_guard_edge_and_via_follow_rules():
    ids = _ids(_ctx())
    assert 'C9' not in ids          # rule 0.3 is stricter than the 0.2 fab floor
    assert 'V1' not in ids          # rule via 0.45 == fab floor
    ids = _ids(_ctx(board_min={'min_track_width': 0.1, 'min_via_diameter': 0.3,
                               'min_copper_edge_clearance': 0.05}))
    assert {'C9', 'V1'} <= ids


def test_guard_via_kinds_from_board_flags():
    assert 'V2' not in _ids(_ctx())
    assert 'V2' in _ids(_ctx(allow_blind=True))
    assert 'V2' not in _ids(_ctx(allow_blind=True, via_span=('F.Cu', 'In1.Cu')))


def test_guard_report_and_strict():
    lines = g.report_lines(_ctx(), strict=False)
    assert lines[0].startswith('Self-imposed limits stricter than the rules (')
    assert not any('UNREASONED' in l for l in lines)
    w1 = next(l for l in lines if l.strip().startswith('W1'))
    assert '0.25 > rule 0.1' in w1 and '[no reason]' in w1
    strict = g.report_lines(_ctx(), strict=True)
    assert strict[-1].strip().startswith('UNREASONED (')
    assert 'W1' in strict[-1] and 'C9' not in strict[-1]
    os.environ['KICAD_RULES_STRICT'] = '1'
    try:
        assert 'UNREASONED' in g.report_lines(_ctx())[-1]
    finally:
        del os.environ['KICAD_RULES_STRICT']


def test_guard_inventory_complete():
    seen = set()
    for l in g.LIMITS:
        assert l['id'] and l['what'] and l['site'] and callable(l['check'])
        assert l['id'] not in seen
        seen.add(l['id'])
    assert all(not l.get('reason') for l in g.unreasoned())


def test_guard_bad_ctx_never_raises():
    assert isinstance(g.evaluate(_ctx(board_min={}, default_width=None, fab={})), list)


def _m(**kw):
    m = dict(vias_in_pads=0, via_types={'through': 10}, via_sizes={'0.6/0.3': 10}, layers_used={'F.Cu': 5},
             width_by_layer={'F.Cu': {'n': 5, 'min': 0.25}},
             fine_entries={'min_width': 0.25, 'at_class_width_where_rule_allows_less': 0},
             necks={'pads': 0}, gap_by_pair={'pad-track': {'n': 5, 'min': 0.2}},
             gaps_on_rule={'pad-track': 0}, slack_by_pair={'pad-track': {'min': 0.0}},
             layers_per_net={'1': 5})
    m.update(kw)
    return m


class _Rules:
    def board_min(self):
        return {'track_width': 0.1, 'via_diameter': 0.4}


def test_envelope_equal_layouts_no_flags():
    assert env.compare(_m(), {'human': _m()}, _Rules()) == []


def test_envelope_flags_what_the_human_used():
    human = _m(vias_in_pads=3, via_types={'through': 5, 'blind': 2}, via_sizes={'0.45/0.2': 5},
               layers_used={'F.Cu': 5, 'In2.Cu': 4}, width_by_layer={'F.Cu': {'n': 5, 'min': 0.1}},
               gap_by_pair={'pad-track': {'n': 5, 'min': 0.1}})
    flags = env.compare(_m(), {'human': human}, _Rules())
    what = {f['what'] for f in flags}
    assert {'via in pad', 'blind via', 'via diameter', 'tracks on In2.Cu', 'min track width F.Cu',
            'min clearance pad-track'} <= what
    width = next(f for f in flags if f['what'] == 'min track width F.Cu')
    assert width['stricter_than_rule'] and width['ours'] == 0.25 and width['rule'] == 0.1


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
    print('ok')
