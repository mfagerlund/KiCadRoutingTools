"""Solder-mask artwork as copper keep-outs.

A graphic drawn on F.Mask or B.Mask is a mask OPENING: whatever copper lies
under it ships bare. A logo, a label or an exposed-copper window drawn there
must therefore not get a track or a via of that side's copper routed through
it -- the router had no notion of the mask layers and routed straight across.

`extract_mask_artwork` turns the artwork of each side into keep-out dicts of
the shape `kicad_parser.extract_keepouts` returns (tracks and vias not
allowed, on F.Cu for F.Mask and B.Cu for B.Mask), so the one native keep-out
rasteriser (`obstacle_map.add_rule_area_keepout_obstacles`) stamps it. Every
graphic of a side is one geometry and the side is their union, so a logo of
thousands of strokes is a handful of polygons. They are kept apart from
`board_info.keepouts` (the board's KiCad rule areas, which DRC, the census
and the rule-area audit read): `routing_keepouts` joins the two for the
consumers that keep NEW copper out, the stamper and the octolinear
smoother's shortcut check. On by default (routing_defaults.MASK_KEEPOUTS);
route.py --no-mask-keepouts opts a run out.

The artwork is read LAZILY, from the board file the first time a router asks
(`board_mask_artwork`), and kept on board_info.mask_artwork: a parse that
never routes pays nothing. The scan starts from each mask `(layer ...)`
token, not from a walk over every footprint. A footprint graphic follows its
footprint's pose in pcb_data, so a part moved in memory carries its artwork.

The artwork is not copper, so the copper keeps MASK_REGISTRATION from it,
not a copper clearance (each entry's `clearance`).

Covered: gr_/fp_ rect, poly, circle, line, arc and text. A filled shape keeps
copper out of its area, an unfilled one out of its stroke. Every emitted
polygon CONTAINS its true shape: a stroke is buffered with mitred joins and
square caps, circles are circumscribed polygons (their holes inscribed), arc
strokes are chord paths widened by the chord's sagitta, and text is a
generous bounding box (KiCad's stroke font: at most _TEXT_EM_PER_CHAR em per
character, 1.6 em line pitch, plus stroke and an italic slant allowance).

Pad openings: every netted pad on the side whose copper (its real, possibly
rotated rectangle) meets the artwork is cut out of it grown by OWN_MARGIN, so
a mask-defined pad or a test point keeps nothing out and a broad opening keeps
copper out everywhere but next to its pads. A pad that is merely near the
artwork exempts nothing, and so does the pad of a net with one pad that no
zone carries: such a net never routes, and cutting its pad out would only let
other nets' copper into the opening. A FILLED copper graphic (a footprint's tab, #1181)
is cut out the same way: the opening exposes copper already there, and that
copper keeps out every net it does not grant.

Scope: the keep-outs reach the consumers that read `routing_keepouts` (the
obstacle stamper, hence every engine that builds a map with it, and the
octolinear smoother). The bridges and welds laid after the search
(copper_keepout_gate) read the rule areas only, and the GUI reads the artwork
from the saved board file.
"""
import math
import os
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]

_MASK_TO_CU = {'F.Mask': 'F.Cu', 'B.Mask': 'B.Cu'}
# Pads use `(layers ...)`; only a graphic carries a singular mask `(layer ..)`
# (quoted since KiCad 6, bare before).
_MASK_LAYER_RE = re.compile(r'\(layer\s+"?([FB]\.Mask)"?[\s)]')
_GRAPHIC_RE = re.compile(r'\((gr|fp)_(rect|poly|circle|line|arc|text)\s')
_NUM = r'(-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)'
_XY_RE = {k: re.compile(r'\(' + k + r'\s+' + _NUM + r'\s+' + _NUM)
          for k in ('start', 'end', 'center', 'mid')}
_AT_RE = re.compile(r'\(at\s+' + _NUM + r'\s+' + _NUM + r'(?:\s+' + _NUM + r')?\s*\)')
_WIDTH_RE = re.compile(r'\(width\s+' + _NUM + r'\s*\)')
_FILL_RE = re.compile(r'\(fill\s+(\w+)')
_SIZE_RE = re.compile(r'\(size\s+' + _NUM + r'\s+' + _NUM + r'\s*\)')
_THICK_RE = re.compile(r'\(thickness\s+' + _NUM + r'\s*\)')
_JUSTIFY_RE = re.compile(r'\(justify([^()]*)\)')
_STR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_CIRCLE_SEGS = 32
_ARC_SEGS = 16
# A graphic within this distance of a netted pad's copper belongs to that pad's
# opening (KiCad's mask expansions and mask-defined pads stay well inside it).
OWN_MARGIN = 0.5
# How far new copper stays from mask artwork: the artwork is no copper, so no
# copper clearance applies, only the mask's registration on the fab (a flush
# edge can come out exposed); typical fab mask registration is +-0.05 mm.
MASK_REGISTRATION = 0.05
# A pad with a negative solder_mask_margin (an exposed pad whose mask opening
# is shrunk inside its copper, HTQFP/QFN thermal pads) is still bridged by
# KiCad's DRC to foreign copper NEAR ITS COPPER, whatever the margin: KiCad 10
# kicad-cli on cm4-lvds-adapter U3 pad 65 (8 x 8 mm, margin -2.35) reports
# solder_mask_bridge for a foreign via 0.175 mm off the pad copper and none at
# 0.18; margins -0.75, -1 and -3.5 the same at 0.17, -0.5 and above clean.
# The band binds every other net to EXPOSED_PAD_REACH off the pad copper.
EXPOSED_PAD_REACH = 0.18
_MASK_MARGIN_RE = re.compile(r'\(solder_mask_margin\s+' + _NUM + r'\s*\)')
_PAD_NUM_RE = re.compile(r'\(pad\s+"((?:[^"\\]|\\.)*)"')
# Widest stroke-font glyph advance (W, M) in em, generous.
_TEXT_EM_PER_CHAR = 1.3


def _filled(block: str) -> bool:
    m = _FILL_RE.search(block)
    return bool(m) and m.group(1) in ('solid', 'yes')


def _stroke(block: str) -> float:
    m = _WIDTH_RE.search(block)
    return float(m.group(1)) if m else 0.0


def _xy(block: str, key: str) -> Optional[Point]:
    m = _XY_RE[key].search(block)
    return (float(m.group(1)), float(m.group(2))) if m else None


def _rot(p: Point, deg: float) -> Point:
    """KiCad's angle (counter-clockwise on screen, y down)."""
    if not deg:
        return p
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (p[0] * c + p[1] * s, -p[0] * s + p[1] * c)


def _rect(x1, y1, x2, y2) -> List[Point]:
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def _circle(c: Point, r: float, outside: bool = True) -> List[Point]:
    """A _CIRCLE_SEGS-gon of the circle: circumscribed (contains the circle)
    when `outside`, else inscribed (inside it, for holes)."""
    if outside:
        r = r / math.cos(math.pi / _CIRCLE_SEGS)
    return [(c[0] + r * math.cos(2 * math.pi * i / _CIRCLE_SEGS),
             c[1] + r * math.sin(2 * math.pi * i / _CIRCLE_SEGS)) for i in range(_CIRCLE_SEGS)]


def _arc_points(s: Point, m: Point, e: Point) -> Tuple[List[Point], float]:
    """(points along the arc, sagitta of one chord between them)."""
    ax, ay = s
    bx, by = m
    cx, cy = e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [s, e], 0.0
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
          + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
          + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a1 = math.atan2(cy - uy, cx - ux)
    sweep = (a1 - a0) % (2 * math.pi)
    if (am - a0) % (2 * math.pi) > sweep:  # the arc runs the other way round
        sweep -= 2 * math.pi
    sag = r * (1.0 - math.cos(abs(sweep) / _ARC_SEGS / 2.0))
    return ([(ux + r * math.cos(a0 + sweep * i / _ARC_SEGS),
              uy + r * math.sin(a0 + sweep * i / _ARC_SEGS)) for i in range(_ARC_SEGS + 1)],
            sag)


def _text_box(block: str) -> Tuple[Optional[Point], float, List[Point]]:
    """(anchor, angle, box corners relative to the anchor before rotation)."""
    from kicad_parser import find_matching_paren
    at = _AT_RE.search(block)
    s = _STR_RE.search(block)
    if not at or not s:
        return None, 0.0, []
    lines = s.group(1).split('\\n')
    eff = block.find('(effects')
    eff_txt = block[eff:find_matching_paren(block, eff)] if eff >= 0 else ''
    sz = _SIZE_RE.search(eff_txt)
    h, w = (float(sz.group(1)), float(sz.group(2))) if sz else (1.0, 1.0)  # (size H W)
    th = _THICK_RE.search(eff_txt)
    pad = (float(th.group(1)) if th else 0.15 * h) / 2.0
    width = max(len(ln) for ln in lines) * w * _TEXT_EM_PER_CHAR + 0.25 * h  # + italic slant
    height = h + (len(lines) - 1) * 1.6 * h
    j = _JUSTIFY_RE.search(eff_txt)
    just = j.group(1).split() if j else []
    if 'mirror' in just:
        just = ['right' if t == 'left' else 'left' if t == 'right' else t for t in just]
    x0 = 0.0 if 'left' in just else -width if 'right' in just else -width / 2
    y0 = 0.0 if 'top' in just else -height if 'bottom' in just else -height / 2
    box = _rect(x0 - pad, y0 - pad, x0 + width + pad, y0 + height + pad)
    return ((float(at.group(1)), float(at.group(2))),
            float(at.group(3) or 0.0), box)


def _geoms(kind: str, block: str, xform) -> list:
    """The shapely geometries (board coordinates) a mask graphic covers, text
    excepted: ONE per graphic, each containing the true shape. A stroke is the
    centre line buffered by half its width with mitred joins and square caps
    (both contain KiCad's round ones); a filled shape is its outline grown by
    the same half width; an arc is its chord path, widened by the chord's
    sagitta. `xform` maps the block's own coordinates to the board's."""
    from shapely.geometry import LineString, Polygon
    from kicad_parser import find_matching_paren, kicad_poly_shape_points
    half = _stroke(block) / 2.0
    filled = _filled(block)

    def stroke(pts, closed=False):
        if half <= 0 or len(pts) < 2:
            return None
        line = LineString(list(pts) + ([pts[0]] if closed else []))
        return line.buffer(half, cap_style='square', join_style='mitre')

    def area(pts):
        g = Polygon(pts)
        if not g.is_valid:
            g = g.buffer(0)
        return g.buffer(half, join_style='mitre') if half > 0 else g

    if kind == 'rect':
        a, b = _xy(block, 'start'), _xy(block, 'end')
        if not a or not b:
            return []
        pts = xform(_rect(a[0], a[1], b[0], b[1]))
        g = area(pts) if filled else stroke(pts, closed=True)
        return [g] if g is not None else []
    if kind == 'circle':
        c, e = _xy(block, 'center'), _xy(block, 'end')
        if not c or not e:
            return []
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        cc = xform([c])[0]
        if filled or r <= half:
            return [Polygon(_circle(cc, r + half))]
        if half <= 0:
            return []
        return [Polygon(_circle(cc, r + half), [_circle(cc, r - half, outside=False)])]
    if kind == 'poly':
        p = block.find('(pts')
        if p < 0:
            return []
        pts = xform(kicad_poly_shape_points(block[p:find_matching_paren(block, p)]))
        if filled and len(pts) >= 3:
            return [area(pts)]
        g = stroke(pts, closed=len(pts) >= 3)
        return [g] if g is not None else []
    if kind == 'line':
        a, b = _xy(block, 'start'), _xy(block, 'end')
        g = stroke(xform([a, b])) if a and b else None
        return [g] if g is not None else []
    if kind == 'arc':
        a, m, b = _xy(block, 'start'), _xy(block, 'mid'), _xy(block, 'end')
        if not (a and m and b) or half <= 0:
            return []
        pts, sag = _arc_points(*xform([a, m, b]))
        return [LineString(pts).buffer(half + sag, cap_style='square', join_style='mitre')]
    return []


def _point_in(p: Point, poly: Sequence[Point]) -> bool:
    x, y = p
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _pad_trees(footprints, copper_graphics=None, routed_nets=None) -> Dict[str, tuple]:
    """{copper side: (STRtree, [copper shape])} of every pad with a net (its
    real size and tilt; round and rounded pads by their enclosing rectangle)
    and every FILLED copper graphic (`copper_graphics`, Segments whose
    `graphic_ring` is set: a SOT89 tab, a filled land): an opening drawn over
    copper exposes that copper, exactly as a pad's own opening does, and the
    copper already keeps every net it does not grant out (#1181)."""
    from shapely import STRtree, affinity
    from shapely.geometry import Polygon, box
    shapes: Dict[str, list] = {'F.Cu': [], 'B.Cu': []}
    nets: Dict[str, list] = {'F.Cu': [], 'B.Cu': []}
    seen_rings = set()
    for seg in copper_graphics or ():
        ring = getattr(seg, 'graphic_ring', None)
        if not ring or len(ring) < 3 or id(ring) in seen_rings:
            continue
        seen_rings.add(id(ring))
        if seg.layer in shapes:
            g = Polygon(ring)
            shapes[seg.layer].append(g if g.is_valid else g.buffer(0))
            nets[seg.layer].append(getattr(seg, 'net_id', 0) or 0)
    # A pad's opening is exempt so its own net can leave it; a net with one
    # pad never routes, and exempting it only opened the band around it to
    # every OTHER net, whose copper there is bare in the opening
    # (solder_mask_bridge: scalenode-cm4 SP6 under the N1 logo).
    npads: Dict[int, int] = {}
    for fp in (footprints or {}).values():
        for pad in getattr(fp, 'pads', ()) or ():
            nid = getattr(pad, 'net_id', 0)
            if nid:
                npads[nid] = npads.get(nid, 0) + 1
    routed = set(routed_nets or ()) | {n for n, c in npads.items() if c > 1}
    for fp in (footprints or {}).values():
        for pad in getattr(fp, 'pads', ()) or ():
            if getattr(pad, 'net_id', 0) not in routed:
                continue
            x, y = pad.global_x, pad.global_y
            hx, hy = pad.size_x / 2.0, pad.size_y / 2.0
            g = box(x - hx, y - hy, x + hx, y + hy)
            rr = getattr(pad, 'rect_rotation', 0.0) or 0.0
            if rr:
                g = affinity.rotate(g, rr, origin=(x, y))
            lays = set(pad.layers or ())
            for cu in ('F.Cu', 'B.Cu'):
                if cu in lays or '*.Cu' in lays or 'F&B.Cu' in lays:
                    shapes[cu].append(g)
                    nets[cu].append(pad.net_id)
    return {cu: (STRtree(gs), gs, nets[cu]) for cu, gs in shapes.items() if gs}


def _exposed_pad_bands(content: str, footprints) -> List[dict]:
    """Opening bands (bands_out dicts) for netted pads with a negative
    solder_mask_margin: the pad copper grown by EXPOSED_PAD_REACH less the
    MASK_REGISTRATION the band's keep-out adds, owned by the pad's net (see
    EXPOSED_PAD_REACH). Pads are found by their margin token, so a board with
    none pays one regex pass."""
    from bisect import bisect_right
    from kicad_parser import find_matching_paren, _footprint_blocks_by_key
    from shapely import affinity
    from shapely.geometry import box
    out: List[dict] = []
    spans = starts = None
    for mm in _MASK_MARGIN_RE.finditer(content):
        if float(mm.group(1)) >= 0.0:
            continue
        ps = content.rfind('(pad ', 0, mm.start())
        if ps < 0 or find_matching_paren(content, ps) <= mm.start():
            continue        # a footprint- or board-level margin, not a pad's
        num = _PAD_NUM_RE.match(content, ps)
        if num is None:
            continue
        if spans is None:
            spans = list(_footprint_blocks_by_key(content))
            starts = [sp[0] for sp in spans]
        i = bisect_right(starts, ps) - 1
        if i < 0 or spans[i][1] <= ps:
            continue
        fp = (footprints or {}).get(spans[i][2])
        for pad in getattr(fp, 'pads', ()) or ():
            if pad.pad_number != num.group(1) or not getattr(pad, 'net_id', 0):
                continue
            x, y = pad.global_x, pad.global_y
            hx, hy = pad.size_x / 2.0, pad.size_y / 2.0
            g = box(x - hx, y - hy, x + hx, y + hy)
            rr = getattr(pad, 'rect_rotation', 0.0) or 0.0
            if rr:
                g = affinity.rotate(g, rr, origin=(x, y))
            g = g.buffer(EXPOSED_PAD_REACH - MASK_REGISTRATION, join_style='mitre')
            lays = set(pad.layers or ())
            for mask, cu in _MASK_TO_CU.items():
                if mask in lays and (cu in lays or '*.Cu' in lays or 'F&B.Cu' in lays):
                    for ring, holes in _rings(g):
                        out.append({'net_id': pad.net_id, 'polygon': ring,
                                    'holes': holes, 'layer': cu, 'mask': mask})
    return out


def _rings(geom):
    """[(outline, holes)] of a shapely (Multi)Polygon, as point lists."""
    polys = getattr(geom, 'geoms', [geom])
    out = []
    for g in polys:
        if g.is_empty or g.geom_type != 'Polygon' or g.area <= 1e-9:
            continue
        out.append((list(g.exterior.coords)[:-1],
                    [list(h.coords)[:-1] for h in g.interiors]))
    return out


def _unowned(geom, pads):
    """The artwork of one side that is no pad's opening (module doc): every
    netted pad whose copper meets it, grown by OWN_MARGIN, is cut out."""
    if pads is None or geom.is_empty:
        return geom
    from shapely import union_all
    tree, shapes = pads[0], pads[1]
    hit = tree.query(geom, predicate='intersects')
    if len(hit) == 0:
        return geom
    grown = union_all([shapes[i].buffer(OWN_MARGIN, join_style='mitre') for i in hit])
    return geom.difference(grown)


def _owned_bands(geom, pads) -> List[Tuple[int, object]]:
    """[(net id, band)]: the part of a side's artwork `_unowned` cuts out,
    split by the net whose copper owns it. Each band is exempt only for its
    own net: copper of ANY other net inside it is bare in the same opening as
    the pad (KiCad solder_mask_bridge; a +3V3 track through the cut-out band
    around a GND capacitor pad under a B.Mask rectangle). Where two nets'
    grown pads overlap (fine-pitch pads in one opening) neither owns it."""
    if pads is None or geom.is_empty:
        return []
    from shapely import union_all
    tree, shapes, nets = pads
    by_net: Dict[int, list] = {}
    for i in tree.query(geom, predicate='intersects'):
        if nets[i]:
            by_net.setdefault(nets[i], []).append(
                shapes[i].buffer(OWN_MARGIN, join_style='mitre'))
    grown = {nid: union_all(g) for nid, g in by_net.items()}
    out = []
    for nid in sorted(grown):
        # only the part no other net's grown pad also covers: at fine pitch
        # a band reaches over the neighbour's copper and refuses its own tap;
        # the shared part stays exempt for every net, as before
        others = [g for n, g in grown.items() if n != nid]
        mine = grown[nid].difference(union_all(others)) if others else grown[nid]
        band = geom.intersection(mine)
        if not band.is_empty and band.area > 1e-9:
            out.append((nid, band))
    return out


def board_mask_artwork(pcb_data) -> List[dict]:
    """The board's mask artwork keep-outs, read once from the board file
    (pcb_data.source_path) the first time a router asks and kept on
    board_info.mask_artwork (None = not read yet), so a parse that never
    routes pays nothing. Footprint artwork takes the footprint's pose from
    pcb_data, so a part moved in memory carries its artwork along."""
    bi = pcb_data.board_info
    art = getattr(bi, 'mask_artwork', None)
    if art is None:
        art = []
        path = getattr(pcb_data, 'source_path', '') or ''
        bands: List[dict] = []
        if path and os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                art = extract_mask_artwork(
                    f.read(), getattr(pcb_data, 'footprints', None),
                    [s for s in getattr(pcb_data, 'segments', ()) or ()
                     if getattr(s, 'graphic', False) and getattr(s, 'graphic_ring', None)],
                    {getattr(z, 'net_id', 0) for z in getattr(pcb_data, 'zones', ()) or ()},
                    bands_out=bands)
        bi.mask_artwork = art
        bi.mask_opening_bands = bands
    return art


def derive_opening_bands(pcb_data) -> List[dict]:
    """Net-filtered keep-outs (net_keepouts regions) for the pad bands the
    mask artwork exempts: each binds every net but the band's owner, so a
    pad's own net still leaves the pad through its opening and no other net's
    copper enters it. [] when the run keeps no mask artwork out (opt-out,
    routing_keepouts) or the board has none. The regions reach every engine
    the net-keep-out guard covers, and its audit reports a breach."""
    bi = getattr(pcb_data, 'board_info', None)
    if bi is None:
        return []
    on = getattr(bi, 'mask_keepouts', None)
    if on is None:
        import routing_defaults
        on = routing_defaults.MASK_KEEPOUTS
    if not on:
        return []
    if getattr(bi, 'mask_artwork', None) is None or not hasattr(bi, 'mask_opening_bands'):
        bi.mask_artwork = None
        board_mask_artwork(pcb_data)
    names = {nid: n.name for nid, n in (getattr(pcb_data, 'nets', None) or {}).items()
             if nid and getattr(n, 'name', '')}
    every = frozenset(names.values())
    out = []
    for b in getattr(bi, 'mask_opening_bands', None) or ():
        own = names.get(b['net_id'], '')
        out.append({'name': '%s opening band %s' % (b['mask'], own or b['net_id']),
                    'rule': None, 'polygon': b['polygon'], 'holes': b['holes'],
                    'layers': {b['layer']}, 'tracks_allowed': False,
                    'vias_allowed': False, 'nets': every - {own},
                    'clearance': MASK_REGISTRATION, 'mask_band': True})
    if not getattr(bi, '_mask_band_said', False):
        bi._mask_band_said = True
        print(f"Mask opening bands: armed ({len(out)} other-net regions)")
    return out


def routing_keepouts(pcb_data) -> List[dict]:
    """Rule-area keep-outs, plus mask artwork unless the run opted out
    (board_info.mask_keepouts, set by batch_route's mask_keepouts; None, as
    every other engine leaves it, is routing_defaults.MASK_KEEPOUTS)."""
    bi = pcb_data.board_info
    keepouts = list(getattr(bi, 'keepouts', None) or [])
    on = getattr(bi, 'mask_keepouts', None)
    if on is None:
        import routing_defaults
        on = routing_defaults.MASK_KEEPOUTS
    if on:
        keepouts += board_mask_artwork(pcb_data)
    return keepouts


def _entry(poly, holes, cu, mask):
    return {'polygon': poly, 'holes': holes, 'layers': {cu},
            'tracks_allowed': False, 'vias_allowed': False,
            'copper_pour_allowed': True, 'pads_allowed': True,
            'footprints_allowed': True, 'in_footprint': False,
            'owner': '', 'name': mask + ' artwork', 'mask_artwork': True,
            # not copper: the copper stays MASK_REGISTRATION clear of it, not
            # a copper clearance (the stamper and the smoother read this)
            'clearance': MASK_REGISTRATION}


def _graphics(content: str, footprints):
    """(shapely geometry, mask layer) per mask graphic, found from its mask
    `(layer ...)` token (a whole-file walk per footprint cost more than the
    artwork itself). A footprint graphic is placed by its footprint's pose."""
    from bisect import bisect_right
    from kicad_parser import (find_matching_paren, footprint_at_match,
                              _footprint_blocks_by_key, local_to_global)
    from shapely.geometry import Polygon
    spans = None
    poses = {}
    seen = set()
    # every graphic start in one linear pass; a mask token belongs to the
    # nearest graphic before it (an rfind per token is quadratic on a board
    # of tens of thousands of mask graphics)
    gstarts, gmatch = [], []
    for m in _GRAPHIC_RE.finditer(content):
        gstarts.append(m.start())
        gmatch.append(m)
    for lm in _MASK_LAYER_RE.finditer(content):
        pos = lm.start()
        gi = bisect_right(gstarts, pos) - 1
        if gi < 0:
            continue
        start, m = gstarts[gi], gmatch[gi]
        if start in seen:
            continue
        end = find_matching_paren(content, start)
        if end <= pos:
            continue          # the layer token is not this graphic's
        seen.add(start)
        block = content[start:end]
        if m.group(1) == 'gr':
            xform = list
        else:
            if spans is None:
                spans = list(_footprint_blocks_by_key(content))
                starts = [sp[0] for sp in spans]
            i = bisect_right(starts, start) - 1
            if i < 0 or spans[i][1] <= start:
                continue
            fs, fe, key = spans[i]
            if fs not in poses:
                fp = (footprints or {}).get(key)
                if fp is not None:
                    poses[fs] = (fp.x, fp.y, fp.rotation or 0.0)
                else:
                    at = footprint_at_match(content[fs:fe])
                    poses[fs] = ((float(at.group(1)), float(at.group(2)),
                                  float(at.group(3) or 0.0)) if at else (0.0, 0.0, 0.0))
            fx, fy, fr = poses[fs]

            def xform(pts, fx=fx, fy=fy, fr=fr):
                return [local_to_global(fx, fy, fr, x, y) for x, y in pts]
        if m.group(2) == 'text':
            anchor, ang, box = _text_box(block)
            if anchor is None:
                continue
            # The text's own angle is absolute in the file (KiCad 6+), so only
            # its anchor follows the footprint pose.
            ax, ay = xform([anchor])[0]
            geoms = [Polygon([(ax + x, ay + y) for x, y in (_rot(p, ang) for p in box)])]
        else:
            geoms = _geoms(m.group(2), block, xform)
        for g in geoms:
            if not g.is_empty:
                yield g, lm.group(1)


def extract_mask_artwork(content: str, footprints=None, copper_graphics=None,
                         routed_nets=None, bands_out: Optional[list] = None) -> List[dict]:
    """Keep-out dicts for the F.Mask / B.Mask artwork of a board (see module
    doc): the union of every graphic on a side, minus the pads' own openings,
    so a logo of many strokes is a few polygons rather than one per stroke.
    `footprints` (parse_kicad_pcb's) supply the pads whose own mask openings
    are exempt, and the footprints' poses; None exempts nothing.
    `copper_graphics` (filled graphic copper Segments) are exempt the same
    way: an opening over a footprint's copper tab is that tab's opening.
    `routed_nets` are nets routed even with a single pad (a zone's net): their
    lone pad keeps its opening exempt too. `bands_out`, when given, receives
    the exempt bands, one dict per net and ring (net_id, polygon, holes,
    layer, mask), for `derive_opening_bands`."""
    from shapely import union_all
    found = {'F.Mask': [], 'B.Mask': []}
    for g, mask in _graphics(content, footprints):
        found[mask].append(g)
    if bands_out is not None:
        bands_out.extend(_exposed_pad_bands(content, footprints))
    if not any(found.values()):
        return []
    pads = _pad_trees(footprints, copper_graphics, routed_nets)
    out = []
    for mask, geoms in found.items():
        if not geoms:
            continue
        cu = _MASK_TO_CU[mask]
        whole = union_all(geoms)
        rest = _unowned(whole, pads.get(cu))
        if bands_out is not None:
            for nid, band in _owned_bands(whole, pads.get(cu)):
                for ring, ring_holes in _rings(band):
                    bands_out.append({'net_id': nid, 'polygon': ring,
                                      'holes': ring_holes, 'layer': cu, 'mask': mask})
        for keep, keep_holes in _rings(rest):
            out.append(_entry(keep, keep_holes, cu, mask))
    return out
