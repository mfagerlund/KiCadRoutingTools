"""Whole-board same-net hole-to-hole guard for a written .kicad_pcb.

Same-net copper is no obstacle, so every via-placing step (main router, plane
taps, stitching, rescue, repair_planes, final reconciliation) can only keep a
new via clear of its OWN net's drills with a step-local check
(`merge_close_same_net_vias` over the step's new vias, the same-net via rings).
Two steps that never see each other's vias -- a write list and a live board
that diverged, a later step reloading the file, a step without the merge -- can
ship two same-net barrels whose drills sit closer than the board's
hole-to-hole minimum. KiCad's hole_to_hole is net-independent, so the pair is
a real DRC error (ecp5-dc-scm: GND through vias 0.11 mm and 0.23 mm apart at
J5.OA3 and J5.A64/B64, drill 0.2, board floor 0.25).

`guard_same_net_via_holes` is the one board-level evaluator, run on the file a
step writes, so it covers every writer at once. For each net it keeps vias in
priority order (locked first, then widest layer span, then file order), with
the net's plated through-hole pads as fixed survivors. A via whose drill lands
within `(d1 + d2) / 2 + hole_to_hole` of a kept same-net hole whose layer span
covers its own is removed; every same-net track end on that via (an end inside
its pad, on a layer of its span) gets a connector to the survivor's centre on
the same layer and width, so connectivity is preserved. The connector lies in
the overlap of the two pads (their centres are closer than one drill plus the
floor, below a pad diameter on any via that clears its own annulus), so it adds
no copper outside what the removed via already occupied on that side. A pair
the survivor cannot cover (blind spans that do not nest) is reported, never
touched. Locked vias are never removed.

Pure text edit: nothing outside the removed via blocks and the inserted
connector segments changes, so the board's version and every other byte round-
trip. Deterministic: connector uuids are derived from their geometry.
"""
import math
import re
import uuid as _uuid

EPS = 1e-4
_NS = _uuid.UUID('8f6f3c1e-6a43-4b59-9d0e-3c6a2f1b7d10')


def _top_level_blocks(text):
    """(start, end, head) of every depth-1 s-expression child of the root,
    end exclusive. Quote-aware so a '(' inside a string does not count."""
    out = []
    depth = 0
    i = 0
    n = len(text)
    in_str = False
    start = None
    while i < n:
        c = text[i]
        if in_str:
            if c == '\\':
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == '(':
            depth += 1
            if depth == 2:
                start = i
        elif c == ')':
            if depth == 2 and start is not None:
                m = re.match(r'\(\s*([A-Za-z_]+)', text[start:start + 40])
                out.append((start, i + 1, m.group(1) if m else ''))
                start = None
            depth -= 1
        i += 1
    return out


_AT = re.compile(r'\(at\s+([-\d.eE+]+)\s+([-\d.eE+]+)')
_START = re.compile(r'\(start\s+([-\d.eE+]+)\s+([-\d.eE+]+)\)')
_END = re.compile(r'\(end\s+([-\d.eE+]+)\s+([-\d.eE+]+)\)')
_NET = re.compile(r'\(net\s+("(?:[^"\\]|\\.)*"|[^\s()]+)(?:\s+"(?:[^"\\]|\\.)*")?\s*\)')
_LAYERS = re.compile(r'\(layers\s+((?:"[^"]*"\s*)+)\)')
_LAYER = re.compile(r'\(layer\s+"([^"]*)"')
_DRILL = re.compile(r'\(drill\s+([-\d.eE+]+)')
_SIZE = re.compile(r'\(size\s+([-\d.eE+]+)')
_WIDTH = re.compile(r'\(width\s+([-\d.eE+]+)')


def _net_key(net_tok, id_to_name):
    """Net identity from a `(net ...)` token: the name in either dialect."""
    if net_tok is None:
        return None
    if net_tok.startswith('"'):
        return net_tok[1:-1]
    try:
        return id_to_name.get(int(net_tok), net_tok)
    except ValueError:
        return net_tok


def _span(layers, kind, cu_order):
    """The copper layers a via ties together, by its type: a through via every
    copper layer, a blind/micro via the run between its two named layers."""
    if kind == 'through' or len(layers) < 2:
        return frozenset(cu_order)
    idx = [cu_order.index(ln) for ln in layers if ln in cu_order]
    if len(idx) < 2:
        return frozenset(layers)
    lo, hi = min(idx), max(idx)
    return frozenset(cu_order[lo:hi + 1])


def find_same_net_hole_violations(text, pcb_data, hole_to_hole):
    """The guard's plan for one board text, without editing it.

    Returns (removals, connectors, unmergeable): removals = [(block_start,
    block_end, via_dict, survivor_xy)], connectors = [segment_dict],
    unmergeable = [(via_dict, other_xy)]."""
    cu_order = list(getattr(pcb_data.board_info, 'copper_layers', None)
                    or ['F.Cu', 'B.Cu'])
    id_to_name = {nid: n.name for nid, n in pcb_data.nets.items()}
    blocks = _top_level_blocks(text)
    vias, segs = [], []
    for order, (s, e, head) in enumerate(blocks):
        if head not in ('via', 'segment', 'arc'):
            continue
        b = text[s:e]
        nm = _NET.search(b)
        net_tok = nm.group(1) if nm else None
        net = _net_key(net_tok, id_to_name)
        if head == 'via':
            at, dr, sz, ly = _AT.search(b), _DRILL.search(b), _SIZE.search(b), _LAYERS.search(b)
            if not (at and dr and net):
                continue
            layers = re.findall(r'"([^"]*)"', ly.group(1)) if ly else ['F.Cu', 'B.Cu']
            kind = ('micro' if re.search(r'\(via\s+micro\b', b) else
                    'blind' if re.search(r'\(via\s+(blind|buried)\b', b) else 'through')
            locked = bool(re.search(r'\(via\s+(?:\w+\s+)*locked\b', b)
                          or re.search(r'\(locked\s+yes\)', b))
            vias.append({'start': s, 'end': e, 'order': order, 'net': net,
                         'net_tok': net_tok,
                         'x': float(at.group(1)), 'y': float(at.group(2)),
                         'drill': float(dr.group(1)),
                         'size': float(sz.group(1)) if sz else float(dr.group(1)),
                         'layers': layers, 'locked': locked,
                         'span': _span(layers, kind, cu_order)})
        elif head == 'segment':
            st, en, w, ly = _START.search(b), _END.search(b), _WIDTH.search(b), _LAYER.search(b)
            if not (st and en and w and ly and net):
                continue
            segs.append({'net': net, 'net_tok': net_tok, 'layer': ly.group(1),
                         'width': float(w.group(1)),
                         'ends': [(float(st.group(1)), float(st.group(2))),
                                  (float(en.group(1)), float(en.group(2)))]})

    from kicad_parser import pad_is_plated_through
    all_cu = frozenset(cu_order)
    kept_by_net = {}
    for nid, pads in (getattr(pcb_data, 'pads_by_net', None) or {}).items():
        name = id_to_name.get(nid)
        if name is None:
            continue
        for p in pads:
            if pad_is_plated_through(p):
                kept_by_net.setdefault(name, []).append(
                    (p.hole_x if getattr(p, 'hole_x', None) is not None else p.global_x,
                     p.hole_y if getattr(p, 'hole_y', None) is not None else p.global_y,
                     p.drill, all_cu, False))

    segs_by_net = {}
    for sg in segs:
        segs_by_net.setdefault(sg['net'], []).append(sg)

    removals, connectors, unmergeable = [], [], []
    for v in sorted(vias, key=lambda v: (not v['locked'], -len(v['span']), v['order'])):
        survivor, blocker = None, None
        for (sx, sy, sd, sspan, _is_via) in kept_by_net.get(v['net'], []):
            if math.hypot(v['x'] - sx, v['y'] - sy) < (v['drill'] + sd) / 2 + hole_to_hole - EPS:
                if v['span'] <= sspan and not v['locked']:
                    survivor = (sx, sy)
                    break
                blocker = (sx, sy)
        if survivor is None:
            if blocker is not None:
                unmergeable.append((v, blocker))
            kept_by_net.setdefault(v['net'], []).append(
                (v['x'], v['y'], v['drill'], v['span'], True))
            continue
        removals.append((v['start'], v['end'], v, survivor))
        r = v['size'] / 2 + EPS
        for sg in segs_by_net.get(v['net'], []):
            if sg['layer'] not in v['span']:
                continue
            for (ex, ey) in sg['ends']:
                if math.hypot(ex - v['x'], ey - v['y']) <= r \
                        and math.hypot(ex - survivor[0], ey - survivor[1]) > EPS:
                    connectors.append({'start': (ex, ey), 'end': survivor,
                                       'width': sg['width'], 'layer': sg['layer'],
                                       'net_tok': sg['net_tok']})
    # one connector per (end, layer): two tracks meeting at the removed via
    # need it once
    seen, uniq = set(), []
    for c in connectors:
        k = (round(c['start'][0], 4), round(c['start'][1], 4), round(c['end'][0], 4),
             round(c['end'][1], 4), c['layer'], c['net_tok'])
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return removals, uniq, unmergeable


def _fmt(v):
    s = f"{v:.6f}".rstrip('0').rstrip('.')
    return s if s not in ('-0', '') else '0'


def _segment_text(c):
    key = (f"{_fmt(c['start'][0])},{_fmt(c['start'][1])},{_fmt(c['end'][0])},"
           f"{_fmt(c['end'][1])},{c['layer']},{c['net_tok']},{_fmt(c['width'])}")
    uid = _uuid.uuid5(_NS, key)
    return (f"(segment (start {_fmt(c['start'][0])} {_fmt(c['start'][1])}) "
            f"(end {_fmt(c['end'][0])} {_fmt(c['end'][1])}) "
            f"(width {_fmt(c['width'])}) (layer \"{c['layer']}\") "
            f"(net {c['net_tok']}) (uuid \"{uid}\"))")


def guard_same_net_via_holes(path, hole_to_hole, out_path=None, pcb_data=None,
                             verbose=True, label='via hole guard'):
    """Apply the guard to the board at `path` (in place unless `out_path`).
    Returns {'removed': n, 'connectors': n, 'unmergeable': n}."""
    if not hole_to_hole or hole_to_hole <= 0:
        return {'removed': 0, 'connectors': 0, 'unmergeable': 0}
    with open(path, encoding='utf-8', newline='') as f:
        text = f.read()
    if pcb_data is None:
        from kicad_parser import parse_kicad_pcb
        pcb_data = parse_kicad_pcb(path)
    removals, connectors, unmergeable = find_same_net_hole_violations(
        text, pcb_data, hole_to_hole)
    if verbose:
        for (_s, _e, v, sv) in removals:
            print(f"  {label}: {v['net']} via at ({v['x']:.3f}, {v['y']:.3f}) drill "
                  f"{v['drill']:g} within hole-to-hole {hole_to_hole:g} mm of same-net "
                  f"hole at ({sv[0]:.3f}, {sv[1]:.3f}): removed, tracks re-anchored")
        for (v, o) in unmergeable:
            print(f"  {label}: WARNING {v['net']} via at ({v['x']:.3f}, {v['y']:.3f}) "
                  f"within hole-to-hole of same-net hole at ({o[0]:.3f}, {o[1]:.3f}) "
                  f"whose layers do not cover it: left in place")
    if removals:
        nl = '\r\n' if '\r\n' in text else '\n'
        cuts = sorted(((s, e) for (s, e, _v, _sv) in removals), reverse=True)
        insert_at = cuts[-1][0]
        # remove each block with the indentation before it and its line end
        for (s, e) in cuts:
            ls = text.rfind('\n', 0, s) + 1
            if text[ls:s].strip() == '':
                s2 = ls
                e2 = e
                if text.startswith(nl, e2):
                    e2 += len(nl)
            else:
                s2, e2 = s, e
            if s == insert_at:
                insert_at = s2
                indent = text[ls:s] if text[ls:s].strip() == '' else '\t'
            text = text[:s2] + text[e2:]
        if connectors:
            add = ''.join(indent + _segment_text(c) + nl for c in connectors)
            text = text[:insert_at] + add + text[insert_at:]
    target = out_path or path
    if removals or (out_path and out_path != path):
        with open(target, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
    return {'removed': len(removals), 'connectors': len(connectors),
            'unmergeable': len(unmergeable)}


def guard_step_output(output_file, hole_to_hole, label='via hole guard'):
    """The CLI hook every via-placing step calls on the board it wrote, after
    its last via-laying pass. Never raises: a guard failure is reported and the
    step's board ships as written (the DRC still grades it)."""
    import os
    if not (output_file and os.path.isfile(output_file)
            and output_file.endswith('.kicad_pcb')):
        return None
    try:
        r = guard_same_net_via_holes(output_file, hole_to_hole, label=label)
        if r['removed'] or r['unmergeable']:
            print(f"  {label}: removed {r['removed']} same-net via(s) inside "
                  f"hole-to-hole {hole_to_hole:g} mm, {r['connectors']} connector(s), "
                  f"{r['unmergeable']} left (layers not covered)")
        return r
    except Exception as e:  # pragma: no cover - reported, never fatal
        print(f"  ({label} skipped: {e})")
        return None


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('input_file')
    ap.add_argument('--output', default=None)
    ap.add_argument('--hole-to-hole-clearance', type=float, default=None,
                    help='drill-edge floor in mm (default: the board rule, via the resolver)')
    a = ap.parse_args(argv)
    h2h = a.hole_to_hole_clearance
    if h2h is None:
        from list_nets import resolve_cli_floor
        import routing_defaults as defaults
        h2h = resolve_cli_floor(a.input_file, 'hole_to_hole', None,
                                defaults.HOLE_TO_HOLE_CLEARANCE, '--hole-to-hole-clearance')
    r = guard_same_net_via_holes(a.input_file, h2h, out_path=a.output)
    print(f"via hole guard: removed {r['removed']}, connectors {r['connectors']}, "
          f"unmergeable {r['unmergeable']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
