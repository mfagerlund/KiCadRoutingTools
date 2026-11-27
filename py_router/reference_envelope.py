#!/usr/bin/env python3
"""Reference envelope: what a board's human layout (and any other router's)
actually used, against what this router used and what the board's rules allow.

    python reference_envelope.py BOARD.reference.kicad_pcb ROUTED.kicad_pcb RULES
        [--other NAME=PATH ...] [--json OUT.json] [--md OUT.md]

RULES is the board (or its .kicad_pro) whose project files carry the rules;
`DesignRules.from_project` answers them with no fab floor, so the numbers are
the board's own. Every variant is measured with the same code:

- track width per net class and per copper layer (min, p5, p50, count);
- clearance used: per copper layer, the gap from every track/via to the
  nearest copper of another net (tracks, vias, pads), as the gap and as the
  slack over the pair's class clearance;
- vias: size/drill pairs and KiCad via types; vias inside a pad's copper;
- necks: a pad whose entering track is narrower than the net's class width,
  and the length of the narrow run from the pad;
- pad entry width by pad pitch (the nearest other pad of the footprint);
- layers per net.

Flags: every feature a reference variant uses that ours never produces, and
every limit where ours is stricter than both the rules and the reference.
A flag reports what the copper shows, not why; the strictness inventory
(docs) carries the why.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict

import numpy as np
import shapely
from shapely.geometry import LineString, Point, box
from shapely import affinity

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kicad_parser import parse_kicad_pcb  # noqa: E402

NEAR_MM = 0.6          # gaps above this are not "used clearance"
HUG_MM = 0.005         # slack at or below this = the gap sits on the rule
PITCH_BUCKETS = ((0.0, 0.5, '<0.5'), (0.5, 0.65, '0.5-0.65'),
                 (0.65, 0.8, '0.65-0.8'), (0.8, 99.0, '>=0.8'))


def _pct(vals, q):
    if not vals:
        return None
    return round(float(np.percentile(np.asarray(vals, float), q)), 4)


def _stats(vals):
    if not vals:
        return {'n': 0}
    a = np.asarray(vals, float)
    return {'n': int(a.size), 'min': round(float(a.min()), 4),
            'p5': round(float(np.percentile(a, 5)), 4),
            'p50': round(float(np.percentile(a, 50)), 4)}


def _bucket(pitch):
    for lo, hi, name in PITCH_BUCKETS:
        if lo <= pitch < hi:
            return name
    return '>=0.8'


def pad_polygon(pad):
    """The pad's copper outline (roundrect/oval rounded, custom as its box)."""
    w, h = float(pad.size_x), float(pad.size_y)
    shape = (pad.shape or '').lower()
    if shape == 'circle':
        return Point(pad.global_x, pad.global_y).buffer(w / 2.0, 16)
    r = 0.0
    if shape == 'oval':
        r = min(w, h) / 2.0
    elif shape == 'roundrect':
        r = min(w, h) * float(getattr(pad, 'roundrect_rratio', 0.0) or 0.0)
    r = min(r, min(w, h) / 2.0 - 1e-6) if r > 0 else 0.0
    core = box(-w / 2 + r, -h / 2 + r, w / 2 - r, h / 2 - r)
    g = core.buffer(r, 8) if r > 0 else core
    rot = float(getattr(pad, 'rect_rotation', 0.0) or 0.0)
    if rot:
        g = affinity.rotate(g, -rot, origin=(0, 0))
    return affinity.translate(g, pad.global_x, pad.global_y)


def pad_copper_layers(pad, copper):
    out = set()
    for l in pad.layers or ():
        if l in ('*.Cu', 'F&B.Cu'):
            out.update(copper if l == '*.Cu' else ('F.Cu', 'B.Cu'))
        elif l.endswith('.Cu'):
            out.add(l)
    if (pad.pad_type or '') == 'np_thru_hole':
        return set()
    return out


def via_copper_layers(via, copper):
    if (via.via_type or 'through') == 'through' or len(via.layers) < 2:
        return set(copper)
    a, b = via.layers[0], via.layers[-1]
    if a in copper and b in copper:
        i, j = sorted((copper.index(a), copper.index(b)))
        return set(copper[i:j + 1])
    return set(copper)


class Rules:
    """The board's own answers, cached per net class."""

    def __init__(self, rules_path, pcb):
        from design_rules import DesignRules
        # from_project reads <stem>.kicad_pro / <stem>.kicad_dru beside RULES
        self.dr = DesignRules.from_project(pcb, rules_path)
        self._w = {}
        self._c = {}

    def klass(self, net_id):
        return self.dr.effective_class(net_id) if net_id else 'Default'

    def width_floor(self, net_id, layer):
        key = (self.klass(net_id), layer)
        if key not in self._w:
            try:
                v = self.dr.floor('track_width', net_id, layer)
            except Exception:
                v = None
            self._w[key] = v
        return self._w[key]

    def class_width(self, net_id):
        try:
            return self.dr.class_value(net_id, 'track_width')
        except Exception:
            return None

    def clearance(self, na, nb):
        ka, kb = self.klass(na), self.klass(nb)
        key = (min(ka, kb), max(ka, kb))
        if key not in self._c:
            vals = [self.dr.board_min.get('clearance') or 0.0]
            for n in (na, nb):
                v = self.dr.class_value(n, 'clearance') if n else None
                if v:
                    vals.append(v)
            self._c[key] = max(vals)
        return self._c[key]

    def board_min(self):
        return dict(self.dr.board_min)


def measure(path, rules_path=None, rules=None):
    pcb = parse_kicad_pcb(path)
    copper = list(pcb.board_info.copper_layers)
    if rules is None:
        rules = Rules(rules_path or path, pcb)
    segs = [s for s in pcb.segments if not getattr(s, 'graphic', False)
            and s.layer in copper]
    vias = list(pcb.vias)
    pads = [p for plist in pcb.pads_by_net.values() for p in plist]
    seen = set()
    upads = []
    for p in pads:
        k = (p.component_ref, p.pad_number, round(p.global_x, 4), round(p.global_y, 4))
        if k not in seen:
            seen.add(k)
            upads.append(p)
    pads = upads
    names = {nid: n.name for nid, n in pcb.nets.items()}
    out = {'board': path, 'copper_layers': copper,
           'counts': {'segments': len(segs), 'vias': len(vias), 'pads': len(pads)}}

    # ---- widths
    by_class = defaultdict(list)
    by_layer = defaultdict(list)
    below_floor = Counter()
    for s in segs:
        by_class[rules.klass(s.net_id)].append(s.width)
        by_layer[s.layer].append(s.width)
        f = rules.width_floor(s.net_id, s.layer)
        if f and s.width < f - 1e-4:
            below_floor[s.layer] += 1
    out['width_by_class'] = {k: _stats(v) for k, v in sorted(by_class.items())}
    out['width_by_layer'] = {k: _stats(v) for k, v in sorted(by_layer.items())}
    out['width_below_rule'] = dict(below_floor)

    # ---- vias
    out['via_sizes'] = {f'{s}/{d}': n for (s, d), n in sorted(
        Counter((round(v.size, 3), round(v.drill, 3)) for v in vias).items())}
    out['via_types'] = dict(Counter(v.via_type or 'through' for v in vias))

    # ---- geometry per layer
    pad_poly = [pad_polygon(p) for p in pads]
    pad_layers = [pad_copper_layers(p, copper) for p in pads]
    items = []   # (layer, geom, net, kind, idx)
    for i, s in enumerate(segs):
        if (s.start_x, s.start_y) == (s.end_x, s.end_y):
            g = Point(s.start_x, s.start_y).buffer(s.width / 2, 8)
        else:
            g = LineString([(s.start_x, s.start_y), (s.end_x, s.end_y)]).buffer(
                s.width / 2, 8)
        items.append((s.layer, g, s.net_id, 'track', i))
    for i, v in enumerate(vias):
        g = Point(v.x, v.y).buffer(v.size / 2, 16)
        for l in via_copper_layers(v, copper):
            items.append((l, g, v.net_id, 'via', i))
    for i, p in enumerate(pads):
        for l in pad_layers[i]:
            # net 0 pads: each its own net
            nid = p.net_id if p.net_id else -(i + 1)
            items.append((l, pad_poly[i], nid, 'pad', i))

    gap_kinds = defaultdict(list)       # 'track-pad' -> gaps
    slack_kinds = defaultdict(list)
    hug = Counter()
    below_rule = Counter()
    per_layer = defaultdict(list)
    for l in copper:
        li = [it for it in items if it[0] == l]
        if not li:
            continue
        geoms = np.array([it[1] for it in li], dtype=object)
        nets = np.array([it[2] for it in li])
        kinds = np.array([it[3] for it in li])
        tree = shapely.STRtree(geoms)
        src = np.where(kinds != 'pad')[0]
        if src.size == 0:
            continue
        a, b = tree.query(geoms[src], predicate='dwithin', distance=NEAR_MM)
        a = src[a]
        m = nets[a] != nets[b]
        # each unordered pair once; pad-sourced pairs never (pads are targets)
        m &= ~((kinds[b] != 'pad') & (a > b))
        a, b = a[m], b[m]
        if a.size == 0:
            continue
        d = shapely.distance(geoms[a], geoms[b])
        # nearest other-net copper per source item and target kind
        best = {}
        for ai, bi, di in zip(a.tolist(), b.tolist(), d.tolist()):
            key = (ai, kinds[bi])
            if key not in best or di < best[key][0]:
                best[key] = (di, bi)
        for (ai, kb), (di, bi) in best.items():
            ka = kinds[ai]
            pair = '-'.join(sorted((ka, kb)))
            req = rules.clearance(max(int(nets[ai]), 0), max(int(nets[bi]), 0))
            sl = di - req
            gap_kinds[pair].append(di)
            slack_kinds[pair].append(sl)
            per_layer[l].append(di)
            if abs(sl) <= HUG_MM:
                hug[pair] += 1
            elif sl < -HUG_MM:
                below_rule[pair] += 1
    out['gap_by_pair'] = {k: _stats(v) for k, v in sorted(gap_kinds.items())}
    out['slack_by_pair'] = {k: _stats(v) for k, v in sorted(slack_kinds.items())}
    out['gaps_on_rule'] = dict(hug)
    out['gaps_below_rule'] = dict(below_rule)
    out['gap_by_layer'] = {k: _stats(v) for k, v in sorted(per_layer.items())}

    # ---- vias in pads (any net; same-net is the feature, other-net would short)
    pad_tree = shapely.STRtree(np.array(pad_poly, dtype=object)) if pads else None
    vip = []
    if pad_tree is not None and vias:
        pts = np.array([Point(v.x, v.y) for v in vias], dtype=object)
        vi, pi = pad_tree.query(pts, predicate='within')
        for a_, b_ in zip(vi.tolist(), pi.tolist()):
            p = pads[b_]
            if (p.pad_type or '') in ('thru_hole', 'np_thru_hole'):
                continue
            vip.append({'via': [round(vias[a_].x, 3), round(vias[a_].y, 3)],
                        'size': vias[a_].size, 'pad': f'{p.component_ref}.{p.pad_number}',
                        'same_net': vias[a_].net_id == p.net_id})
    out['vias_in_pads'] = len([v for v in vip if v['same_net']])
    out['vias_in_pads_sample'] = vip[:10]

    # ---- pad pitch and entry width, necks
    by_fp = defaultdict(list)
    for i, p in enumerate(pads):
        by_fp[p.component_ref].append(i)
    pitch = [99.0] * len(pads)
    for ref, idx in by_fp.items():
        if len(idx) < 2:
            continue
        xy = np.array([(pads[i].global_x, pads[i].global_y) for i in idx])
        dd = np.hypot(xy[:, None, 0] - xy[None, :, 0], xy[:, None, 1] - xy[None, :, 1])
        dd[dd < 1e-3] = 99.0
        mins = dd.min(axis=1)
        for k, i in enumerate(idx):
            pitch[i] = float(mins[k])
    # segment endpoints inside pads (same net)
    ends = []
    for si, s in enumerate(segs):
        ends.append((Point(s.start_x, s.start_y), si, 0))
        ends.append((Point(s.end_x, s.end_y), si, 1))
    entry = defaultdict(list)          # bucket -> widths
    entry_fine_cls = []                # (entry width, class width, floor) at pitch < 0.65
    necks = []
    adj = defaultdict(list)            # endpoint key -> segment idx (for neck length)
    for si, s in enumerate(segs):
        adj[(round(s.start_x, 3), round(s.start_y, 3), s.layer)].append(si)
        adj[(round(s.end_x, 3), round(s.end_y, 3), s.layer)].append(si)
    if pad_tree is not None and ends:
        pts = np.array([e[0] for e in ends], dtype=object)
        ei, pi = pad_tree.query(pts, predicate='within')
        done = set()
        for e_, p_ in zip(ei.tolist(), pi.tolist()):
            _, si, which = ends[e_]
            s = segs[si]
            p = pads[p_]
            if s.net_id != p.net_id or s.layer not in pad_layers[p_]:
                continue
            if (si, p_) in done:
                continue
            done.add((si, p_))
            b = _bucket(pitch[p_])
            entry[b].append(s.width)
            cw = rules.class_width(s.net_id)
            fl = rules.width_floor(s.net_id, s.layer)
            if pitch[p_] < 0.65:
                entry_fine_cls.append((s.width, cw, fl))
            if cw and s.width < cw - 1e-4:
                # walk the narrow run from the pad
                length, cur, seen_s = 0.0, si, set()
                x, y = ((s.start_x, s.start_y) if which == 0 else (s.end_x, s.end_y))
                while cur is not None and cur not in seen_s:
                    seen_s.add(cur)
                    c = segs[cur]
                    if c.width >= cw - 1e-4:
                        break
                    length += math.hypot(c.end_x - c.start_x, c.end_y - c.start_y)
                    if (round(c.start_x, 3), round(c.start_y, 3)) == (round(x, 3), round(y, 3)):
                        x, y = c.end_x, c.end_y
                    else:
                        x, y = c.start_x, c.start_y
                    nxt = [k for k in adj[(round(x, 3), round(y, 3), c.layer)]
                           if k not in seen_s]
                    cur = nxt[0] if len(nxt) == 1 else None
                necks.append({'len': round(length, 3), 'width': s.width,
                              'pitch': round(pitch[p_], 3)})
    out['entry_width_by_pitch'] = {k: _stats(v) for k, v in sorted(entry.items())}
    fine_at_class = sum(1 for w, cw, fl in entry_fine_cls
                        if cw and fl and w >= cw - 1e-4 and fl < cw - 1e-4)
    out['fine_entries'] = {
        'n': len(entry_fine_cls),
        'at_class_width_where_rule_allows_less': fine_at_class,
        'min_width': min((w for w, _, _ in entry_fine_cls), default=None)}
    nl = [n['len'] for n in necks]
    out['necks'] = {'pads': len(necks), 'len': _stats(nl),
                    'min_width': min((n['width'] for n in necks), default=None)}

    # ---- layers per net
    net_layers = defaultdict(set)
    for s in segs:
        net_layers[s.net_id].add(s.layer)
    out['layers_per_net'] = dict(sorted(Counter(len(v) for v in net_layers.values()).items()))
    out['layers_used'] = dict(sorted(Counter(s.layer for s in segs).items()))
    out['net_names'] = len(names)
    return out, rules


def compare(ours, refs, rules):
    """Flags: features a reference uses that ours never produces, and limits
    where ours is stricter than both the rules and the reference."""
    flags = []
    bm = rules.board_min()
    for name, ref in refs.items():
        # vias in pads
        if ref['vias_in_pads'] and not ours['vias_in_pads']:
            flags.append({'kind': 'feature', 'what': 'via in pad', 'ref': name,
                          'ref_value': ref['vias_in_pads'], 'ours': 0})
        # via types
        for t, n in ref['via_types'].items():
            if t != 'through' and not ours['via_types'].get(t):
                flags.append({'kind': 'feature', 'what': f'{t} via', 'ref': name,
                              'ref_value': n, 'ours': 0})
        # smallest via
        def smallest(vs):
            sz = [tuple(map(float, k.split('/'))) for k in vs]
            return min(sz) if sz else None
        rs, os_ = smallest(ref['via_sizes']), smallest(ours['via_sizes'])
        if rs and os_ and rs[0] < os_[0] - 1e-3:
            rule = bm.get('via_diameter')
            flags.append({'kind': 'limit', 'what': 'via diameter', 'ref': name,
                          'ref_value': rs, 'ours': os_, 'rule': rule,
                          'stricter_than_rule': bool(rule is None or os_[0] > rule + 1e-3)})
        # layers used
        for l, n in ref['layers_used'].items():
            if not ours['layers_used'].get(l):
                flags.append({'kind': 'feature', 'what': f'tracks on {l}', 'ref': name,
                              'ref_value': n, 'ours': 0})
        # track width per layer
        for l, st in ref['width_by_layer'].items():
            o = ours['width_by_layer'].get(l)
            if not o or not o.get('n') or not st.get('n'):
                continue
            if st['min'] < o['min'] - 1e-3:
                rule = bm.get('track_width')
                flags.append({'kind': 'limit', 'what': f'min track width {l}', 'ref': name,
                              'ref_value': st['min'], 'ours': o['min'], 'rule': rule,
                              'stricter_than_rule': bool(rule is None or o['min'] > rule + 1e-3)})
        # fine-pitch entries
        rf, of = ref['fine_entries'], ours['fine_entries']
        if rf['min_width'] and of['min_width'] and rf['min_width'] < of['min_width'] - 1e-3:
            flags.append({'kind': 'limit', 'what': 'fine-pitch pad entry width', 'ref': name,
                          'ref_value': rf['min_width'], 'ours': of['min_width'],
                          'ours_at_class_width': of['at_class_width_where_rule_allows_less']})
        # necks
        if ref['necks']['pads'] and ref['necks']['pads'] > 2 * max(1, ours['necks']['pads']):
            flags.append({'kind': 'feature', 'what': 'necked pad entries', 'ref': name,
                          'ref_value': ref['necks']['pads'], 'ours': ours['necks']['pads']})
        # clearance per pair kind
        for pair, st in ref['gap_by_pair'].items():
            o = ours['gap_by_pair'].get(pair)
            if not o or not o.get('n') or not st.get('n'):
                continue
            ro = ref['gaps_on_rule'].get(pair, 0)
            oo = ours['gaps_on_rule'].get(pair, 0)
            if st['min'] < o['min'] - 0.01:
                flags.append({'kind': 'limit', 'what': f'min clearance {pair}', 'ref': name,
                              'ref_value': st['min'], 'ours': o['min'],
                              'ref_on_rule': ro, 'ours_on_rule': oo,
                              'ours_slack_min': ours['slack_by_pair'][pair]['min']})
        # multi-layer nets
        rl = sum(n for k, n in ref['layers_per_net'].items() if int(k) >= 3)
        ol = sum(n for k, n in ours['layers_per_net'].items() if int(k) >= 3)
        if rl > 2 * max(1, ol):
            flags.append({'kind': 'feature', 'what': 'nets on 3+ layers', 'ref': name,
                          'ref_value': rl, 'ours': ol})
    return flags


def run(reference, routed, rules_path, others=()):
    ref, rules = measure(reference, rules_path)
    ours, _ = measure(routed, rules=rules)
    refs = {'human': ref}
    for name, p in others:
        refs[name], _ = measure(p, rules=rules)
    return {'rules': {'source': rules_path, 'board_min': rules.board_min(),
                      'classes': {k: {kk: vv for kk, vv in v.items()
                                      if kk in ('clearance', 'track_width', 'via_diameter',
                                                'via_drill')}
                                  for k, v in rules.dr.classes.items()}},
            'ours': ours, **{f'ref_{k}': v for k, v in refs.items()},
            'flags': compare(ours, refs, rules)}


def to_md(res, title=''):
    L = [f'## {title}' if title else '## envelope', '']
    bm = res['rules']['board_min']
    L.append('rules: ' + ', '.join(f'{k} {v}' for k, v in sorted(bm.items())))
    for name in [k for k in res if k == 'ours' or k.startswith('ref_')]:
        m = res[name]
        L.append(f"- **{name}**: vias {sum(m['via_sizes'].values())} "
                 f"{m['via_sizes']} types {m['via_types']}; via-in-pad {m['vias_in_pads']}; "
                 f"necks {m['necks']['pads']}; fine entries {m['fine_entries']}; "
                 f"layers {m['layers_used']}")
        L.append('  - width/layer: ' + '; '.join(
            f"{l} min {s.get('min')} p5 {s.get('p5')}" for l, s in m['width_by_layer'].items()))
        L.append('  - gap/pair: ' + '; '.join(
            f"{p} min {s.get('min')} (on rule {m['gaps_on_rule'].get(p, 0)}, "
            f"below {m['gaps_below_rule'].get(p, 0)})" for p, s in m['gap_by_pair'].items()))
    L.append('')
    for f in res['flags']:
        L.append(f"- FLAG {f['kind']}: {f['what']} ({f['ref']} {f['ref_value']} vs ours {f['ours']})"
                 + (f" stricter than rule {f.get('rule')}" if f.get('stricter_than_rule') else ''))
    return '\n'.join(L) + '\n'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('reference')
    ap.add_argument('routed')
    ap.add_argument('rules')
    ap.add_argument('--other', action='append', default=[],
                    help='NAME=PATH, another routed board to measure (e.g. koppar=...)')
    ap.add_argument('--json')
    ap.add_argument('--md')
    a = ap.parse_args(argv)
    others = [tuple(o.split('=', 1)) for o in a.other]
    res = run(a.reference, a.routed, a.rules, others)
    if a.json:
        with open(a.json, 'w') as f:
            json.dump(res, f, indent=1)
    md = to_md(res, os.path.basename(a.reference))
    if a.md:
        open(a.md, 'w').write(md)
    print(md)
    return 0


if __name__ == '__main__':
    sys.exit(main())
