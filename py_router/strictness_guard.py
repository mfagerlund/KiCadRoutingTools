"""Self-imposed limits stricter than the board's rules (strictness audit, 2026-11-27).

The router is only allowed to be stricter than the board's DRC rules for a written reason. This module holds the
inventory of every place it is, and reports, at the start of a route, the ones that bite on the board at hand:

    Self-imposed limits stricter than the rules (N):
      W1  only power/impedance nets neck down: class 0.2 > rule 0.1  [no reason]
      C9  board edge clearance pinned: 0.2 > rule 0.075  [#441 ...]

A limit with ``reason=None`` has none on record. Strict mode (``KICAD_RULES_STRICT=1`` or ``strict=True``)
lists those separately as ``UNREASONED``; :func:`unreasoned` returns them for a gate.

Each entry: ``id``, ``what``, ``site`` (file:line in this repo), ``reason`` (issue / text, or None), and
``check(ctx) -> (router_value, rule_value) | True | None``: the values when the limit is stricter than this
board's rule, True when it is stricter on every board (a geometry restriction KiCad does not have), None when it
does not bite here.

``ctx`` (built by :func:`context`): board_min (DesignRules.board_min), default_width, default_clearance,
class_widths, pair_gaps, grid, via_size, via_drill, fab (fab_floors dict), copper_count, allow_blind,
allow_micro, via_span.
"""
import json
import os

EPS = 1e-6


def _lt(rule, router):
    """(router, rule) when the rule allows less than the router uses."""
    if rule is None or router is None:
        return None
    return (router, rule) if rule < router - EPS else None


def _bm(ctx, key):
    return ctx['board_min'].get(key)


LIMITS = [
    # ---- track width
    dict(id='W1', what='only power/impedance nets neck down; every other net routes at its class width',
         site='single_ended_routing.py:4746-4750', reason=None,
         check=lambda c: _lt(_bm(c, 'min_track_width'), c['default_width'])),
    dict(id='W2', what='a necking net stops at the Default class width, not the rule floor',
         site='single_ended_routing.py:8563-8573 (_neck_width_for_net)', reason=None,
         check=lambda c: _lt(_bm(c, 'min_track_width'), c['default_width'])),
    dict(id='W3', what='escape reserve and fine escape sized at the class width',
         site='escape_reserve.py:267,554; fine_escape.py:192', reason=None,
         check=lambda c: _lt(_bm(c, 'min_track_width'), c['default_width'])),
    dict(id='W4', what='net rescue rung 0 and width march stop above the rule floor',
         site='net_rescue.py:576-578,597-603,2155-2161', reason=None,
         check=lambda c: _lt(_bm(c, 'min_track_width'), c['default_width'])),
    dict(id='W5', what='terminal graze, plane tap and BGA fan-out neck floor = max(fab tier width, rule)',
         site='single_ended_routing.py:1496-1508,1735-1738; plane_pad_tap.py:239,247; '
              'bga_fanout/__init__.py:3940-3946',
         reason='#176/#223: fab tier track floor',
         check=lambda c: _lt(_bm(c, 'min_track_width'), c['fab'].get('track_width'))),
    dict(id='W6', what='repair pass minimum width 0.2', site='routing_defaults.py:327', reason=None,
         check=lambda c: _lt(_bm(c, 'min_track_width'), 0.2)),
    # ---- clearance (grid inflation and pinned floors)
    dict(id='C1', what='via keep-out and via edge band rounded up to whole grid cells',
         site='obstacle_map.py:379,410,1357,1588-1594,4504-4507; obstacle_cache.py:1010-1013',
         reason='#170: a cell is blocked when any via centre in it could violate',
         check=lambda c: (round(c['default_clearance'] + c['grid'], 4), c['default_clearance'])),
    dict(id='C2', what='via diagonal margin +0.25 cell (constant documented in mm, used in cells)',
         site='obstacle_map.py:4559,4566; routing_defaults.py:34', reason=None,
         check=lambda c: (round(c['default_clearance'] + 0.25 * c['grid'], 4), c['default_clearance'])),
    dict(id='C3', what='via-to-via spacing at least one cell', site='obstacle_map.py:558', reason=None,
         check=lambda c: True),
    dict(id='C4', what='pad corner buffer +grid/2', site='routing_utils.py:206-207,243,309-310,345',
         reason=None,
         check=lambda c: (round(c['default_clearance'] + c['grid'] / 2, 4), c['default_clearance'])),
    dict(id='C5', what='diff-pair corner margin 0.75 cell', site='obstacle_map.py:5048,5115', reason=None,
         check=lambda c: True if c['pair_gaps'] else None),
    dict(id='C6', what='obstacles held at the widest class clearance in a batch',
         site='obstacle_map.py:184-195; routing_config.py:819-831', reason='#208/#309: one obstacle map per batch',
         check=lambda c: True if len(set(c['class_clearances'])) > 1 else None),
    dict(id='C7', what='wide-net margin snapped up to the lattice', site='routing_config.py:40-71', reason=None,
         check=lambda c: True if len(set(c['class_widths'])) > 1 else None),
    dict(id='C8', what='track to NPTH floor max(clearance, 0.20, hole clearance)',
         site='obstacle_map.py:2769-2770; routing_defaults.py:232', reason='#233: JLC NPTH-to-track 0.20',
         check=lambda c: _lt(max(c['default_clearance'], _bm(c, 'min_hole_clearance') or 0), 0.20)),
    dict(id='C9', what='board edge clearance pinned to the fab floor',
         site='fix_kicad_drc_settings.py:569-570; route.py:2995-2999; fab_tiers.py:121-135,1196-1199',
         reason='#441: fab board-edge floor 0.20',
         check=lambda c: _lt(_bm(c, 'min_copper_edge_clearance'), c['fab'].get('board_edge'))),
    dict(id='C11', what='hole-to-hole default and pin 0.20', site='route.py:11499-11501',
         reason='fab tier hole_to_hole 0.20',
         check=lambda c: _lt(_bm(c, 'min_hole_to_hole'), 0.20)),
    dict(id='C12', what='diff-pair gap floor max(gap, Default clearance)',
         site='route_diff.py:843-846,1221-1229', reason=None,
         check=lambda c: _lt(min(c['pair_gaps']) if c['pair_gaps'] else None, c['default_clearance'])),
    dict(id='C14', what='same-net THT hole-to-hole +1 cell', site='obstacle_map.py:4237-4272', reason=None,
         check=lambda c: True),
    # ---- vias
    dict(id='V1', what='via descent stops at the fab tier floor',
         site='fab_tiers.py:714-742,1336', reason='fab tier via floor (advanced only on escalation)',
         check=lambda c: _lt(_bm(c, 'min_via_diameter'), c['fab'].get('via_diameter'))
         or _lt(_bm(c, 'min_through_hole_diameter'), c['fab'].get('via_drill'))),
    dict(id='V2', what='blind/buried/micro vias only with --via-span; the board allowing them is not read',
         site='via_span.py; routing_config.py:320; route.py:10975-10989', reason='one via kind per step',
         check=lambda c: True if (c['allow_blind'] or c['allow_micro']) and not c['via_span'] else None),
    dict(id='V3', what='no via in pad (QFN fan-out off, main router only as fallback)',
         site='qfn_fanout/__init__.py:3378; obstacle_map.py:4014,4049; single_ended_routing.py:3957',
         reason=None, check=lambda c: True),
    dict(id='V4', what='via annular ring at the fab tier recommended value',
         site='fab_tiers.py:893,1375,1392', reason='#1914: fab annular ring',
         check=lambda c: _lt(_bm(c, 'min_via_annular_width'), c['fab'].get('annular'))),
    dict(id='V5', what='drill >= board thickness / aspect ratio', site='fab_tiers.py:145,671',
         reason='#502: plating aspect ratio', check=lambda c: None),
    dict(id='V6', what='BGA dog-bone via 0.5/0.3',
         site='routing_defaults.py:249-250; bga_fanout/__init__.py:3182,3276,4001,5923', reason=None,
         check=lambda c: _lt(_bm(c, 'min_via_diameter'), 0.5)),
    dict(id='V7', what='no vias inside a BGA field unless a via span is given',
         site='routing_config.py:198; route.py:3340-3347', reason='BGA fan-out owns the field',
         check=lambda c: None if c['via_span'] else True),
    # ---- geometry
    dict(id='G1', what='main pass on a 0.1 mm grid; finer only in rescue', site='routing_defaults.py:27',
         reason='runtime', check=lambda c: True),
    dict(id='G2', what='8 directions only (KiCad allows any angle)',
         site='rust_router/src/lane_search.rs:21; rust_router/src/pair_search.rs:26', reason=None,
         check=lambda c: True),
    dict(id='G3', what='minimum turning radius 0.2', site='routing_config.py:232', reason=None,
         check=lambda c: True),
    dict(id='G4', what='pad-entry neck length 2.5 / taper 2.0', site='routing_defaults.py:64,73', reason=None,
         check=lambda c: True),
    # ---- layers
    dict(id='L1', what='BGA fan-out and diff pairs default to F.Cu/B.Cu',
         site='bga_fanout/__init__.py:5834; route_diff.py:3560', reason=None,
         check=lambda c: True if c['copper_count'] > 2 else None),
    dict(id='L3', what='plane layers forbidden to signal tracks', site='routing_config.py:1236-1265',
         reason=None, check=lambda c: True if c['copper_count'] > 2 else None),
]


def _fmt(v):
    return f'{v:g}' if isinstance(v, float) else str(v)


def evaluate(ctx):
    """[(limit, values)] for every limit stricter than this board's rules."""
    out = []
    for lim in LIMITS:
        try:
            v = lim['check'](ctx)
        except (KeyError, TypeError, ValueError):
            v = None
        if v:
            out.append((lim, v))
    return out


def unreasoned(hits=None):
    """The limits (of ``hits``, or all) with no reason on record."""
    pool = [l for l, _ in hits] if hits is not None else LIMITS
    return [l for l in pool if not l.get('reason')]


def report_lines(ctx, strict=None):
    if strict is None:
        strict = os.environ.get('KICAD_RULES_STRICT', '') not in ('', '0')
    hits = evaluate(ctx)
    lines = [f'Self-imposed limits stricter than the rules ({len(hits)}):']
    for lim, v in hits:
        val = '' if v is True else f': {_fmt(v[0])} > rule {_fmt(v[1])}'
        lines.append(f"  {lim['id']:<4}{lim['what']}{val}  [{lim['reason'] or 'no reason'}]  ({lim['site']})")
    if strict:
        bad = unreasoned(hits)
        lines.append(f'  UNREASONED ({len(bad)}): ' + (', '.join(l['id'] for l in bad) or 'none'))
    return lines


def _pro_rules(pro_path):
    try:
        with open(pro_path, encoding='utf-8') as f:
            return (json.load(f).get('board', {}).get('design_settings', {}).get('rules', {})) or {}
    except (OSError, ValueError):
        return {}


def context(dr, pro_path=None, grid=0.1, via_size=None, via_drill=None, fab=None, copper_count=2,
            via_span=None):
    """ctx for :func:`evaluate` from a DesignRules and the board's .kicad_pro."""
    classes = dr.classes or {}
    d = classes.get('Default', {})
    pro = _pro_rules(pro_path) if pro_path else {}
    return dict(
        board_min=dict(dr.board_min or {}),
        default_width=d.get('track_width'), default_clearance=d.get('clearance') or 0.0,
        class_widths=[c['track_width'] for c in classes.values() if c.get('track_width')],
        class_clearances=[c['clearance'] for c in classes.values() if c.get('clearance')],
        pair_gaps=[c['diff_pair_gap'] for c in classes.values() if c.get('diff_pair_gap')],
        grid=grid, via_size=via_size, via_drill=via_drill, fab=dict(fab or {}), copper_count=copper_count,
        allow_blind=bool(pro.get('allow_blind_buried_vias')), allow_micro=bool(pro.get('allow_microvias')),
        via_span=via_span)
