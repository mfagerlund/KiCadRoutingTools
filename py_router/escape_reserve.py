"""Escape reservation: plan every fine-pitch pin's dog-bone before routing.

On a fine-pitch part the room just outside the pins holds one via site per
pin and no more. The main pass routes net by net, and the first nets to
route lay their tracks and vias through the sites their neighbours needed:
on a dense QFN or BGA the later pins find their exit already taken and end
unrouted although a legal fanout for every pin exists.

This module plans that fanout jointly, part by part, before the obstacle
maps are built. A pin is FINE when its gap to the nearest other-net pad of
its own footprint is narrower than one track plus two clearances (no track
passes between the pads). Each fine pin gets one escape: a stub on its pad
layer and a via.

* Peripheral pins (QFN, QFP, fine connectors): the via sits outward along
  the pin's exit axis, just past the row, with lateral offsets of half a
  pitch either way so that adjacent pins stagger their vias.
* BGA balls (a grid of round pads): the via sits at one of the four
  diagonal midpoints between the ball and its neighbours.

Every candidate is checked geometrically against the board as it is (pads
of other nets on every layer the via spans, other nets' tracks and vias,
the vias and stubs already planned for other pins, the drill-to-drill rule
and the board edge). The pins of a part are planned most constrained first;
a pin prefers the site that leans toward its net's destination (the
centroid of the net's pads off the part), so the exits of one side come out
ordered the way their tracks will leave.

The planned stubs and vias go into pcb_data as copper of their (unrouted)
net: for every other net they are hard keep-outs, and the main pass routes
the net from its via (own-net copper is a route terminal). The reservations
ride on fine_escape's bracket (`begin` / `finish` / `release`): a successful
route takes its stub and via, a net that never routes has them released
after the routing passes, so nothing ships unconnected. A net with a pour
zone on the board gets a plane via, kept at release (the via is the pin's
plane connection). A power-like pin whose net has no zone is reported, never
given an invented zone.
"""
import math
import os
import re

import fine_escape as fe
from via_span import via_layer_pair, via_kind

# Depth steps past the row and lateral offsets (in half pitches) tried per pin.
DEPTH_STEPS = 6
LATERAL_HALVES = 3
POWER_NAME = re.compile(r'^(GND|AGND|DGND|PGND|VSS|VCC|VDD|VBUS|VBAT|VSYS|VIN|'
                        r'[+-]?\d+V\d*|\+?\d*V\d+|V\d+V\d*)', re.I)


# ---- geometry -------------------------------------------------------------

def _pt_box(px, py, cx, cy, hx, hy):
    dx = max(abs(px - cx) - hx, 0.0)
    dy = max(abs(py - cy) - hy, 0.0)
    return math.hypot(dx, dy)


def _pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segs_cross(a, b, c, d):
    d1, d2 = _cross(c, d, a), _cross(c, d, b)
    d3, d4 = _cross(a, b, c), _cross(a, b, d)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def _seg_seg(a, b, c, d):
    if _segs_cross(a, b, c, d):
        return 0.0
    return min(_pt_seg(*a, *c, *d), _pt_seg(*b, *c, *d),
               _pt_seg(*c, *a, *b), _pt_seg(*d, *a, *b))


def _in_poly(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        (xi, yi), (xj, yj) = poly[i], poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _poly_near(a, b, r, poly):
    """Segment a-b (a point when a == b) inside `poly` or within r of its edge."""
    if _in_poly(a[0], a[1], poly) or _in_poly(b[0], b[1], poly):
        return True
    return any(_seg_seg(a, b, poly[i - 1], poly[i]) < r - 1e-9
               for i in range(len(poly)))


def _ko_on(ko, layer):
    """Does keep-out `ko` cover copper layer `layer` (None = any layer)?"""
    kl = ko.get('layers') or set()
    if not kl or layer is None or '*.Cu' in kl or layer in kl:
        return True
    return layer in ('F.Cu', 'B.Cu') and bool({'F&B.Cu', 'F&B'} & set(kl))


def _seg_box(a, b, cx, cy, hx, hy):
    if _pt_box(*a, cx, cy, hx, hy) == 0.0 or _pt_box(*b, cx, cy, hx, hy) == 0.0:
        return 0.0
    cs = [(cx - hx, cy - hy), (cx + hx, cy - hy), (cx + hx, cy + hy), (cx - hx, cy + hy)]
    return min(_seg_seg(a, b, cs[i], cs[(i + 1) % 4]) for i in range(4))


def _pad_geom(q):
    """(cx, cy, hx, hy, r): a box (r = 0) or a circle (hx = hy = r)."""
    hx, hy = fe._half_extents(q)
    if q.shape == 'circle':
        r = min(hx, hy)
        return q.global_x, q.global_y, 0.0, 0.0, r
    return q.global_x, q.global_y, hx, hy, 0.0


def _pad_dist_pt(q, x, y):
    cx, cy, hx, hy, r = _pad_geom(q)
    return max(_pt_box(x, y, cx, cy, hx, hy) - r, 0.0)


def _pad_dist_seg(q, a, b):
    cx, cy, hx, hy, r = _pad_geom(q)
    return max(_seg_box(a, b, cx, cy, hx, hy) - r, 0.0)


def _pad_gap(p, q):
    """Edge-to-edge gap between two pads (boxes; a circle as its box)."""
    phx, phy = fe._half_extents(p)
    qhx, qhy = fe._half_extents(q)
    dx = max(abs(p.global_x - q.global_x) - phx - qhx, 0.0)
    dy = max(abs(p.global_y - q.global_y) - phy - qhy, 0.0)
    return math.hypot(dx, dy)


# ---- obstacles ------------------------------------------------------------

class _Board:
    """The copper a candidate must clear, gathered once per plan."""

    def __init__(self, pcb_data, config):
        self.pcb = pcb_data
        self.cfg = config
        self.pads = fe._all_pads(pcb_data)
        self.vias = list(pcb_data.vias)        # (other nets filtered per test)
        self.segs = list(pcb_data.segments)
        self.planned = []                       # copper planned in this run
        bi = pcb_data.board_info
        bb = bi.board_bounds if bi else None
        self.bounds = bb
        # The board's own rules the router enforces on its copper: keep-out
        # rule areas, the real outline and its cutouts (not only the box).
        self.keepouts = [k for k in (getattr(bi, 'keepouts', None) or [])
                         if len(k.get('polygon') or []) >= 3
                         and not (k.get('tracks_allowed', True)
                                  and k.get('vias_allowed', True))]
        outs = list(getattr(bi, 'board_outlines', None) or [])
        if not outs and getattr(bi, 'board_outline', None):
            outs = [bi.board_outline]
        self.outlines = [o for o in outs if len(o) >= 3]
        self.cutouts = [c for c in (getattr(bi, 'board_cutouts', None) or [])
                        if len(c) >= 3]
        self._nclr = {}
        self.edge = config.board_edge_clearance or config.clearance
        # The board's copper-to-HOLE floor (min_hole_clearance): a new via's
        # drill is held that far off every other net's copper, which binds
        # over ring + clearance when the drill is large against the ring.
        try:
            from obstacle_map import resolve_hole_clearance
            self.hole = float(resolve_hole_clearance(pcb_data, config) or 0.0)
        except Exception:
            self.hole = float(getattr(config, 'hole_clearance', 0.0) or 0.0)

    def _hole_need(self, ring_need, extra=0.0):
        """Centre distance a new via keeps from other-net copper: the ring
        at `ring_need`, or the drill at the hole floor, whichever is more."""
        dr = self.cfg.via_drill / 2
        return max(ring_need + extra, dr + self.hole)

    def _near(self, items, x, y, span, key):
        return [o for o in items if abs(key(o)[0] - x) <= span and abs(key(o)[1] - y) <= span]

    def _clr(self, net_id, clr):
        """Clearance between this copper and net `net_id`'s: the larger of
        the two nets' clearances (KiCad grades a pair at the larger)."""
        c = self._nclr.get(net_id)
        if c is None:
            c = self._nclr[net_id] = self.cfg.get_net_clearance(net_id)
        return c if c > clr else clr

    def _off_rules(self, a, b, r, layer, via):
        """Copper a-b of half-width r (a via: a == b, layer None) breaks a
        keep-out, leaves the outline or enters a cutout."""
        for ko in self.keepouts:
            if via and ko.get('vias_allowed', True):
                continue
            if not via and (ko.get('tracks_allowed', True) or not _ko_on(ko, layer)):
                continue
            if _poly_near(a, b, r, ko['polygon']):
                return True
        m = r + self.edge
        for c in self.cutouts:
            if _poly_near(a, b, m, c):
                return True
        if self.outlines:
            inside = [o for o in self.outlines
                      if _in_poly(a[0], a[1], o) and _in_poly(b[0], b[1], o)]
            if not inside:
                return True
            if all(any(_seg_seg(a, b, o[i - 1], o[i]) < m - 1e-9
                       for i in range(len(o))) for o in inside):
                return True
        return False

    def via_ok(self, net_id, x, y, clr):
        cfg = self.cfg
        vr, dr = cfg.via_size / 2, cfg.via_drill / 2
        if self.bounds:
            x0, y0, x1, y1 = self.bounds
            m = vr + self.edge
            if x < x0 + m or x > x1 - m or y < y0 + m or y > y1 - m:
                return False
        if self._off_rules((x, y), (x, y), vr, None, True):
            return False
        span = 3.0
        h2h = cfg.hole_to_hole_clearance
        for q in self.pads:
            hx, hy = fe._half_extents(q)
            if abs(q.global_x - x) > span + hx or abs(q.global_y - y) > span + hy:
                continue
            qd = getattr(q, 'drill', 0) or 0
            if qd > 0 and math.hypot(q.global_x - x, q.global_y - y) < dr + qd / 2 + h2h - 1e-9:
                return False
            if q.net_id == net_id:
                continue
            if not any(l in cfg.layers or l == '*.Cu' for l in q.layers):
                continue
            qc = self._clr(q.net_id, clr)
            need = self._hole_need(vr + qc, self.cfg.via_hole_extra(qc, pad=q))
            if _pad_dist_pt(q, x, y) < need - 1e-9:
                return False
        for v in self.vias + [o for o in self.planned if o[0] == 'via']:
            vx, vy, vsize, vdrill, vnet = (v.x, v.y, v.size, v.drill, v.net_id) \
                if not isinstance(v, tuple) else v[1:]
            if abs(vx - x) > span or abs(vy - y) > span:
                continue
            d = math.hypot(vx - x, vy - y)
            if d < dr + vdrill / 2 + h2h - 1e-9:
                return False
            # Another net's via: ring to ring at clearance, and each drill
            # off the other's ring at the hole floor.
            if vnet != net_id and d < max(vr + vsize / 2 + self._clr(vnet, clr),
                                          dr + vsize / 2 + self.hole,
                                          vr + vdrill / 2 + self.hole) - 1e-9:
                return False
        for s in self.segs + [o for o in self.planned if o[0] == 'seg']:
            if isinstance(s, tuple):
                _, a, b, w, layer, snet = s
            else:
                a, b, w, layer, snet = ((s.start_x, s.start_y), (s.end_x, s.end_y),
                                        s.width, s.layer, s.net_id)
            if snet == net_id or layer not in cfg.layers:
                continue
            if (min(a[0], b[0]) > x + span or max(a[0], b[0]) < x - span
                    or min(a[1], b[1]) > y + span or max(a[1], b[1]) < y - span):
                continue
            sc = self._clr(snet, clr)
            need = self._hole_need(vr + sc, self.cfg.via_hole_extra(sc))
            if _pt_seg(x, y, *a, *b) < need + w / 2 - 1e-9:
                return False
        return True

    def stub_ok(self, net_id, a, b, w, layer, clr, own_pad):
        hw = w / 2
        if self._off_rules(a, b, hw, layer, False):
            return False
        span = 3.0 + math.hypot(b[0] - a[0], b[1] - a[1])
        for q in self.pads:
            if q is own_pad or q.net_id == net_id or not fe._on_layer(q, layer):
                continue
            hx, hy = fe._half_extents(q)
            if abs(q.global_x - a[0]) > span + hx or abs(q.global_y - a[1]) > span + hy:
                continue
            if _pad_dist_seg(q, a, b) < hw + self._clr(q.net_id, clr) - 1e-9:
                return False
        for v in self.vias + [o for o in self.planned if o[0] == 'via']:
            vx, vy, vsize, _, vnet = (v.x, v.y, v.size, v.drill, v.net_id) \
                if not isinstance(v, tuple) else v[1:]
            if vnet == net_id or abs(vx - a[0]) > span or abs(vy - a[1]) > span:
                continue
            if _pt_seg(vx, vy, *a, *b) < vsize / 2 + hw + self._clr(vnet, clr) - 1e-9:
                return False
        for s in self.segs + [o for o in self.planned if o[0] == 'seg']:
            if isinstance(s, tuple):
                _, c, d, sw, sl, snet = s
            else:
                c, d, sw, sl, snet = ((s.start_x, s.start_y), (s.end_x, s.end_y),
                                      s.width, s.layer, s.net_id)
            if snet == net_id or sl != layer:
                continue
            if (min(c[0], d[0]) > a[0] + span or max(c[0], d[0]) < a[0] - span
                    or min(c[1], d[1]) > a[1] + span or max(c[1], d[1]) < a[1] - span):
                continue
            if _seg_seg(a, b, c, d) < hw + sw / 2 + self._clr(snet, clr) - 1e-9:
                return False
        return True


# ---- candidates -----------------------------------------------------------

def _is_bga(pads):
    smd = [p for p in pads if not (getattr(p, 'drill', 0) or 0) > 0]
    if len(smd) < 9 or any(p.shape != 'circle' for p in smd):
        return False
    xs = {round(p.global_x, 3) for p in smd}
    ys = {round(p.global_y, 3) for p in smd}
    return len(xs) >= 3 and len(ys) >= 3


def _fine_pins(pcb_data, config, net_ids, by_fp):
    """(ref, pads of the footprint, [fine pins of the wanted nets], bga)."""
    wanted = set(net_ids)
    refs = sorted({p.component_ref for nid in wanted
                   for p in pcb_data.pads_by_net.get(nid, [])})
    out = []
    for ref in refs:
        pads = by_fp.get(ref, [])
        smd = [p for p in pads if not (getattr(p, 'drill', 0) or 0) > 0]
        if len(smd) < 4:
            continue
        fine = []
        for p in smd:
            if p.net_id not in wanted:
                continue
            layer = next((l for l in p.layers if l in config.layers), None)
            if layer is None:
                continue
            w = config.get_net_track_width(p.net_id, layer)
            clr = config.get_net_clearance(p.net_id)
            gaps = [_pad_gap(p, q) for q in smd
                    if q is not p and q.net_id != p.net_id and fe._on_layer(q, layer)]
            if gaps and min(gaps) < w + 2 * clr - 1e-9:
                fine.append(p)
        if fine:
            out.append((ref, pads, fine, _is_bga(smd)))
    return out


def _local_mm():
    """Same-layer reach (mm) below which a row pin's net counts as local;
    KICAD_ESCAPE_LOCAL_MM < 0 reserves every fine row pin."""
    try:
        return float(os.environ.get('KICAD_ESCAPE_LOCAL_MM', '3.0'))
    except ValueError:
        return 3.0


def _needs_via(pcb_data, p, layer, zones, local_mm):
    """A row pin skips its reservation only when its net is local: a pad of
    the net off p's footprint sits on `layer` within `local_mm`, so the pin
    leaves the row by a short stub (decoupling, an adjacent part) and a
    reserved via would only block its neighbours' exits. Pour nets always
    keep theirs (the via is the plane drop)."""
    if local_mm < 0 or p.net_id in zones:
        return True
    for q in pcb_data.pads_by_net.get(p.net_id, []):
        if (q.component_ref != p.component_ref and fe._on_layer(q, layer)
                and math.hypot(q.global_x - p.global_x,
                               q.global_y - p.global_y) <= local_mm):
            return False
    return True


def _flanked(p, pads, zones):
    """True when p's two row neighbours (the nearest pads, on opposite sides)
    both belong to one other net with no pour: that net joins its pins along
    the row edge (a pour net drops to its plane instead), walling p in
    unless p's exit is reserved first."""
    others = [q for q in pads if q is not p and not (getattr(q, 'drill', 0) or 0) > 0]
    if len(others) < 2:
        return False
    d = lambda q: math.hypot(q.global_x - p.global_x, q.global_y - p.global_y)
    pitch = min(d(q) for q in others)
    near = [q for q in others if d(q) <= pitch * 1.05]
    if len(near) != 2:
        return False
    a, b = near
    if (a.net_id != b.net_id or not a.net_id or a.net_id == p.net_id
            or a.net_id in zones):
        return False
    dot = ((a.global_x - p.global_x) * (b.global_x - p.global_x)
           + (a.global_y - p.global_y) * (b.global_y - p.global_y))
    return dot < 0


def _dest(pcb_data, p):
    """Centroid of p's net pads off p's footprint, or None."""
    qs = [q for q in pcb_data.pads_by_net.get(p.net_id, [])
          if q.component_ref != p.component_ref]
    if not qs:
        return None
    return (sum(q.global_x for q in qs) / len(qs), sum(q.global_y for q in qs) / len(qs))


def _snap(v, g):
    return round(round(v / g) * g, 6)


def _snap_out(v, g, sgn):
    """Snap to the grid, never back toward the row (direction sgn)."""
    q = v / g
    q = math.ceil(q - 1e-9) if sgn > 0 else math.floor(q + 1e-9)
    return round(q * g, 6)


def _min_pitch(pads):
    best = None
    for i, a in enumerate(pads):
        for b in pads[i + 1:]:
            d = math.hypot(a.global_x - b.global_x, a.global_y - b.global_y)
            if d > 1e-6 and (best is None or d < best):
                best = d
    return best or 0.0


def _row_geom(p, pads, config, w, clr):
    """The pin's exit: axis (horiz), outward sign, its lateral coordinate,
    the row's outer edge along the axis, the pitch, the bend coordinate just
    past the row and the first via depth."""
    cx = sum(q.global_x for q in pads) / len(pads)
    cy = sum(q.global_y for q in pads) / len(pads)
    phx, phy = fe._half_extents(p)
    if phx > fe.ELONGATED * phy:
        horiz = True
    elif phy > fe.ELONGATED * phx:
        horiz = False
    else:
        horiz = abs(p.global_x - cx) >= abs(p.global_y - cy)
    sgn = 1 if ((p.global_x - cx) if horiz else (p.global_y - cy)) >= 0 else -1
    a0 = p.global_x if horiz else p.global_y
    pc = p.global_y if horiz else p.global_x
    half_a = phx if horiz else phy
    pitch = None
    edge = a0 + sgn * half_a
    for q in pads:
        if q is p:
            continue
        qa = q.global_x if horiz else q.global_y
        qp = q.global_y if horiz else q.global_x
        qhx, qhy = fe._half_extents(q)
        qha = qhx if horiz else qhy
        if abs(qa - a0) < half_a + qha:  # same row
            d = abs(qp - pc)
            if d > 1e-6:
                pitch = d if pitch is None else min(pitch, d)
            if d < 3.0:
                far = qa + sgn * qha
                edge = max(edge, far) if sgn > 0 else min(edge, far)
    pitch = pitch or (2 * max(phx, phy))
    g = config.grid_step
    vr = config.via_size / 2
    return {'horiz': horiz, 'sgn': sgn, 'pc': pc, 'a0': a0, 'edge': edge,
            'pitch': pitch,
            'bend': _snap_out(edge + sgn * (clr + w / 2), g, sgn),
            'd0': _snap_out(edge + sgn * (clr + vr), g, sgn),
            'key': (horiz, sgn, round(a0, 2))}


def _xy(geo, a, lat):
    return (a, lat) if geo['horiz'] else (lat, a)


def _bone(p, geo, depth, lat, g):
    """Stub points and via for a via at (depth, lat): straight out of the
    row to the bend, a 45-degree leg to the via's lateral, then straight."""
    sgn, pc, bend = geo['sgn'], geo['pc'], geo['bend']
    lat = _snap(lat, g)
    start = (p.global_x, p.global_y)
    L = abs(lat - pc)
    if L < 1e-6:
        via = _xy(geo, depth, pc)
        return via, [start, via]
    knee = _snap_out(bend + sgn * L, g, sgn)
    depth = depth if sgn * (depth - knee) >= 0 else knee
    pts = [start, _xy(geo, bend, pc), _xy(geo, knee, lat)]
    via = _xy(geo, depth, lat)
    if abs(depth - knee) > 1e-6:
        pts.append(via)
    return via, pts


def _single_cands(p, geo, config, dest):
    """Ordered [(cost, via_xy, [stub points])] for one row pin."""
    g = config.grid_step
    pitch = geo['pitch']
    lean = 0.0
    if dest is not None:
        lean = (dest[1] if geo['horiz'] else dest[0]) - geo['pc']
    out = []
    steps = max(DEPTH_STEPS, int(math.ceil(2 * config.via_size / g)))
    for k in range(steps + 1):
        depth = geo['d0'] + geo['sgn'] * k * g
        for m in range(-LATERAL_HALVES, LATERAL_HALVES + 1):
            via, pts = _bone(p, geo, depth, geo['pc'] + m * pitch / 2, g)
            cost = k * g / pitch + 0.6 * abs(m)
            if lean and m:
                cost -= 0.3 * (1 if (m > 0) == (lean > 0) else -1)
            out.append((cost, via, pts))
    out.sort(key=lambda c: c[0])
    return out


def _row_patterns(row, config, lean):
    """Joint layouts for one row (pins sorted along it): each a list of
    (via, pts) in row order. Inline, staggered, and fanned at 45 degrees
    around the row centre (and shifted toward the row's destinations)."""
    g = config.grid_step
    geo0 = row[0][1]
    sgn, pitch, d0 = geo0['sgn'], geo0['pitch'], geo0['d0']
    sp = max(config.via_size + config.clearance,
             config.via_drill + config.hole_to_hole_clearance)
    n = len(row)
    pats = []
    if pitch >= sp - 1e-6:
        pats.append([_bone(p, geo, d0, geo['pc'], g) for p, geo in row])
    dz = _snap_out(math.sqrt(max(sp * sp - pitch * pitch, 0.0)), g, 1)
    for phase in (0, 1):
        pats.append([_bone(p, geo, d0 + sgn * dz * ((i + phase) % 2), geo['pc'], g)
                     for i, (p, geo) in enumerate(row)])
    sf = _snap_out(max(sp, pitch), g, 1)
    c = sum(geo['pc'] for _, geo in row) / n
    spread = (n - 1) / 2 * (sf - pitch)
    shifts = [0.0]
    if lean:
        shifts.append(math.copysign(min(abs(lean), spread + sf), lean))
    # The middle vias sit beside their neighbours' bends: push the whole fan
    # out until it clears; at the far end every via lies on one line past the
    # longest 45-degree leg.
    reach = (n - 1) / 2 * (sf - pitch) + (abs(shifts[-1]) if lean else 0.0)
    for off in range(0, int(math.ceil((reach + config.via_size) / g)) + 1):
        for sh in shifts:
            y0 = _snap(c + sh - (n - 1) / 2 * sf, g)
            pats.append([_bone(p, geo, d0 + sgn * off * g, y0 + i * sf, g)
                         for i, (p, geo) in enumerate(row)])
    return pats


def _bga_cands(p, pads, config, dest):
    """Ordered [(cost, via_xy, [stub points])]: the 4 diagonal midpoints."""
    g = config.grid_step
    best = None
    for q in pads:
        if q is p:
            continue
        d = math.hypot(q.global_x - p.global_x, q.global_y - p.global_y)
        if d > 1e-6 and (best is None or d < best):
            best = d
    pitch = best or 1.0
    h = pitch / 2
    out = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            vx, vy = p.global_x + sx * h, p.global_y + sy * h
            cost = 0.0
            if dest is not None:
                cost = -((dest[0] - p.global_x) * sx + (dest[1] - p.global_y) * sy)
            sv = (_snap(vx, g), _snap(vy, g))
            out.append((cost, sv, [(p.global_x, p.global_y), sv]))
            raw = (round(vx, 6), round(vy, 6))
            if sv != raw:
                out.append((cost + 0.5, raw, [(p.global_x, p.global_y), raw]))
    out.sort(key=lambda c: c[0])
    return out


def _legal(board, p, via, pts, w, clr, layer):
    if not board.via_ok(p.net_id, via[0], via[1], clr):
        return False
    for a, b in zip(pts, pts[1:]):
        if not board.stub_ok(p.net_id, a, b, w, layer, clr, p):
            return False
    return True


def _commit(board, config, planned, p, via, pts, layer, w):
    board.planned.append(('via', via[0], via[1], config.via_size,
                          config.via_drill, p.net_id))
    for a, b in zip(pts, pts[1:]):
        board.planned.append(('seg', a, b, w, layer, p.net_id))
    entry = (p, via, pts, layer, w)
    planned.setdefault(p.net_id, []).append(entry)
    return entry


def _withdraw(board, config, planned, entry):
    """Undo one `_commit`: drop its items from the board and the plan."""
    p, via, pts, layer, w = entry
    items = [('via', via[0], via[1], config.via_size, config.via_drill, p.net_id)]
    items += [('seg', a, b, w, layer, p.net_id) for a, b in zip(pts, pts[1:])]
    for it in items:
        board.planned.remove(it)
    mine = planned[p.net_id]
    mine[:] = [e for e in mine if e is not entry]
    if not mine:
        del planned[p.net_id]


def _lone_bones(row, got, config):
    """Row indices whose dog-bone walls in an adjacent pin of another net
    that found no site of its own (`got[k]` is None). Only where the row is
    too tight for that pin's track to pass a via between the two pins: the
    via then blocks the pin's straight exit, and the pin would route better
    with no reservation beside it."""
    out = []
    for i, ((j, geo), e) in enumerate(zip(row, got)):
        if e is None:
            continue
        for k in (i - 1, i + 1):
            if not 0 <= k < len(row) or got[k] is not None:
                continue
            jk, gk = row[k]
            if jk[0].net_id == j[0].net_id or jk[0].net_id <= 0:
                continue
            if abs(gk['pc'] - geo['pc']) > 1.5 * geo['pitch']:
                continue  # an unreserved pin lies between them
            pass_pitch = config.via_size / 2 + jk[3] + jk[2] / 2
            if geo['pitch'] < pass_pitch - 1e-9:
                out.append(i)
                break
    return out


def plan(pcb_data, config, net_ids, by_fp=None, only_pads=None, verbose=False,
         weak=()):
    """Plan dog-bones for the fine pins of `net_ids`; pins of `weak` nets
    only when flanked (`_flanked`). Returns
    ({net_id: [(pad, via_xy, [stub points], layer, width)]}, [pins with no
    legal site]). `only_pads` limits the plan to those pads (a single net
    re-planned against the copper present now)."""
    by_fp = by_fp if by_fp is not None else fe._footprints(pcb_data)
    board = _Board(pcb_data, config)
    planned, unplaced = {}, []
    parts = _fine_pins(pcb_data, config, net_ids, by_fp)
    zones = _zone_nets(pcb_data)
    local_mm = _local_mm()
    # BGAs first (every inner ball needs its via), then rows finest first: a
    # net that already holds a reserved via at a finer part reaches every
    # layer there, so its pins on a coarser row escape without a second one.
    parts.sort(key=lambda t: (not t[3], _min_pitch(t[1]), t[0]))
    for ref, pads, fine, bga in parts:
        jobs = []
        for p in fine:
            if only_pads is not None and p not in only_pads:
                continue
            if fe._has_own_copper(pcb_data, p):
                continue  # already escaped (pre-existing route or stub)
            if not bga and only_pads is None and p.net_id in planned:
                continue
            layer = next(l for l in p.layers if l in config.layers)
            if not bga and not _flanked(p, pads, zones):
                if p.net_id in weak:
                    continue
                if not _needs_via(pcb_data, p, layer, zones, local_mm):
                    continue
            w = config.get_net_track_width(p.net_id, layer)
            clr = config.get_net_clearance(p.net_id)
            jobs.append((p, layer, w, clr, _dest(pcb_data, p)))
        if bga:
            cx = sum(q.global_x for q in pads) / len(pads)
            cy = sum(q.global_y for q in pads) / len(pads)
            # innermost balls first: they have the fewest ways out
            jobs.sort(key=lambda j: math.hypot(j[0].global_x - cx, j[0].global_y - cy))
            for p, layer, w, clr, dest in jobs:
                pick = next(((v, pts) for _, v, pts in _bga_cands(p, pads, config, dest)
                             if _legal(board, p, v, pts, w, clr, layer)), None)
                if pick is None:
                    unplaced.append(p)
                else:
                    _commit(board, config, planned, p, pick[0], pick[1], layer, w)
            continue
        rows = {}
        for j in jobs:
            geo = _row_geom(j[0], pads, config, j[2], j[3])
            rows.setdefault(geo['key'], []).append((j, geo))
        for key in sorted(rows, key=str):
            row = sorted(rows[key], key=lambda r: r[1]['pc'])
            leans = [((j[4][1] if r_geo['horiz'] else j[4][0]) - r_geo['pc'])
                     for j, r_geo in row if j[4] is not None]
            lean = sum(leans) / len(leans) if leans else 0.0
            best = None
            if len(row) > 1:
                for pat in _row_patterns([(j[0], geo) for j, geo in row], config, lean):
                    mark = len(board.planned)
                    ok = []
                    for (j, geo), (via, pts) in zip(row, pat):
                        p, layer, w, clr, _ = j
                        if _legal(board, p, via, pts, w, clr, layer):
                            board.planned.append(('via', via[0], via[1], config.via_size,
                                                  config.via_drill, p.net_id))
                            for a, b in zip(pts, pts[1:]):
                                board.planned.append(('seg', a, b, w, layer, p.net_id))
                            ok.append(True)
                        else:
                            ok.append(False)
                    del board.planned[mark:]
                    if best is None or sum(ok) > sum(best[1]):
                        best = (pat, ok)
                    if all(ok):
                        break
            got = [None] * len(row)
            rest = []
            for idx, (j, geo) in enumerate(row):
                p, layer, w, clr, dest = j
                if best is not None and best[1][idx]:
                    via, pts = best[0][idx]
                    if _legal(board, p, via, pts, w, clr, layer):
                        got[idx] = _commit(board, config, planned, p, via, pts, layer, w)
                        continue
                rest.append(idx)
            for idx in rest:
                (j, geo) = row[idx]
                p, layer, w, clr, dest = j
                pick = next(((v, pts) for _, v, pts in _single_cands(p, geo, config, dest)
                             if _legal(board, p, v, pts, w, clr, layer)), None)
                if pick is None:
                    unplaced.append(p)
                else:
                    got[idx] = _commit(board, config, planned, p, pick[0], pick[1], layer, w)
            # One pass, no cascade: a withdrawn pin still routes on its own
            # layer, so it walls nobody in and triggers no further withdrawal.
            for idx in _lone_bones(row, got, config):
                _withdraw(board, config, planned, got[idx])
    return planned, unplaced


def _copper(pcb_data, config, net_id, items):
    """Lay the planned items of one net into pcb_data; returns the objects."""
    from kicad_parser import Segment, Via
    out = []
    for p, (vx, vy), pts, layer, w in items:
        for a, b in zip(pts, pts[1:]):
            if math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-6:
                continue
            s = Segment(start_x=a[0], start_y=a[1], end_x=b[0], end_y=b[1],
                        width=w, layer=layer, net_id=net_id)
            pcb_data.segments.append(s)
            out.append(s)
        v = Via(x=vx, y=vy, size=config.via_size, drill=config.via_drill,
                layers=via_layer_pair(config), net_id=net_id,
                via_type=via_kind(config))
        pcb_data.vias.append(v)
        out.append(v)
    return out


def _zone_nets(pcb_data):
    from kicad_parser import pour_zones
    return {z.net_id for z in pour_zones(getattr(pcb_data, 'zones', []) or [])}


def lay_net(pcb_data, st, net_id, config):
    """fine_escape's fresh-lay hook for a dog-bone net: re-plan its fine pins
    against the copper present now."""
    from pcb_modification import bump_copper_epoch
    pads = [p for p in pcb_data.pads_by_net.get(net_id, [])]
    planned, _ = plan(pcb_data, config, [net_id], st.by_fp, only_pads=pads)
    objs = _copper(pcb_data, config, net_id, planned.get(net_id, []))
    if objs:
        bump_copper_epoch(pcb_data)
        st.laid += len(objs)
    return objs


def escape_reserve_prepass(pcb_data, config, net_ids, net_clearances=None,
                           verbose=False):
    """Plan and reserve the dog-bones of every fine pin of `net_ids`. Call
    before the obstacle maps are built. Returns the number of nets reserved."""
    from pcb_modification import bump_copper_epoch
    st = getattr(pcb_data, fe._STATE, None)
    if st is None:
        st = fe._State(set(), fe._footprints(pcb_data), fe._all_pads(pcb_data),
                       net_clearances, verbose)
        setattr(pcb_data, fe._STATE, st)
    net_ids = [n for n in net_ids if n not in st.pending]
    zones = _zone_nets(pcb_data)
    names = {p.net_id: p.net_name for ps in pcb_data.pads_by_net.values() for p in ps}
    # A power-like net with no pour has no plane to drop into: its pins route
    # like signals, and a reservation it fails to use only blocks neighbours.
    # Exception: a pin walled in by its row neighbours keeps its reservation.
    weak = {n for n in net_ids
            if n not in zones and POWER_NAME.match(names.get(n) or '')}
    nozone = sorted({names.get(n) or '' for n in weak})
    fines = _fine_pins(pcb_data, config, [n for n in net_ids if n not in weak],
                       st.by_fp)
    planned, unplaced = plan(pcb_data, config, net_ids, st.by_fp, verbose=verbose,
                             weak=weak)
    n_pins = sum(len(v) for v in planned.values())
    laid = 0
    nets = set()
    for nid in sorted(planned):
        objs = _copper(pcb_data, config, nid, planned[nid])
        if objs:
            st.pending[nid] = objs
            nets.add(nid)
            laid += len(objs)
            if nid in zones:
                st.plane.add(nid)
    st.nets |= nets
    st.dogbone |= nets
    st.laid += laid
    if laid:
        bump_copper_epoch(pcb_data)
    print(f"Escape reserve: {sum(len(f) for _, _, f, _ in fines)} fine pin(s) on "
          f"{len(fines)} part(s); {n_pins} dog-bone(s) reserved on "
          f"{len(planned)} net(s), {len(unplaced)} pin(s) without a legal site")
    if nozone:
        print(f"Escape reserve: power-like net(s) with no zone, not reserved "
              f"(routed as signals): {', '.join(n for n in nozone if n)}")
    if verbose:
        for p in unplaced:
            print(f"  escape reserve: {p.component_ref}.{p.pad_number} "
                  f"({p.net_name}) no legal via site")
    return len(planned)
