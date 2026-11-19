"""Legal-site escape search for a boxed SMD pad: a pad-layer stub, straight
or one 45-degree dogleg, out to a via site that holds every rule.

The #666 bare-ball escape tries a via AT the ball (via-in-pad / dogbone at
the pitch offset). In a fine-pitch connector row whose neighbours are already
routed, that pocket is usually consumed: the via has no room next to the
pad, but a corridor a millimetre or two away often has. This module walks
octilinear stubs out of the pad on its own layer, puts a via at each stub
end, and grades every candidate EXACTLY against the copper on the board:

  * foreign pads by their true copper (check_drc.segment_pad_gap /
    point_to_pad_distance -- the DRC's own shape), priced at the pair
    clearance the DRC grades;
  * foreign tracks on the stub's layer, and on every layer for the via;
  * foreign vias (stack clearance), and every drill on the board at the
    hole-to-hole minimum (net-blind, #671), and copper-to-hole
    (config.hole_clearance).

Each candidate carries the list of blockers instead of a bool: a pad, a
drill or a keep-out is HARD (nothing in a post-route pass moves it); a
foreign track or via is MOVABLE -- its net could re-route around the new
stub. `find_stub_via_sites` returns the candidates ranked: blocker-free
first, then by the number of movable blocker nets, then by stub length.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from net_rescue import _pt_seg_d, _seg_seg_d

# Longest stub the search walks (mm, along the path). A boxed connector
# pad's free corridor is typically one track pitch or two past the row;
# farther than this the gap route itself is the better tool.
MAX_STUB_MM = 2.0
# Candidates kept per pad (ranked); the shift pass looks at the top few.
MAX_CANDIDATES = 40
# Nearest-first search stops once this many blocker-free sites are found
# and the walk is FREE_SLACK_MM past the first of them.
FREE_ENOUGH = 8
FREE_SLACK_MM = 0.3


@dataclass
class Blocker:
    kind: str          # 'pad' | 'drill' | 'seg' | 'via' | 'keepout'
    net_id: int
    obj: object = None

    @property
    def hard(self) -> bool:
        return self.kind in ('pad', 'drill', 'keepout')


@dataclass
class StubSite:
    pad: object
    layer: str
    path: List[Tuple[float, float]]   # pad centre .. via centre
    width: float
    via_size: float
    via_drill: float
    blockers: List[Blocker] = field(default_factory=list)

    @property
    def length(self) -> float:
        return sum(math.hypot(b[0] - a[0], b[1] - a[1])
                   for a, b in zip(self.path, self.path[1:]))

    @property
    def via_xy(self) -> Tuple[float, float]:
        return self.path[-1]

    @property
    def hard(self) -> bool:
        return any(b.hard for b in self.blockers)

    @property
    def blocker_nets(self) -> frozenset:
        return frozenset(b.net_id for b in self.blockers if not b.hard)

    def rank_key(self):
        return (self.hard, len(self.blocker_nets),
                len(self.blockers), round(self.length, 4))

    def describe(self, pcb_data=None) -> str:
        def _n(nid):
            if pcb_data is not None and nid in pcb_data.nets:
                return pcb_data.nets[nid].name
            return str(nid)
        x, y = self.via_xy
        bl = ', '.join(sorted({f"{b.kind}:{_n(b.net_id)}"
                               for b in self.blockers})) or 'clear'
        return (f"stub {len(self.path) - 1} leg(s) {self.length:.2f}mm -> via "
                f"{self.via_size:.2f}/{self.via_drill:.2f} at ({x:.3f},{y:.3f})"
                f" [{bl}]")


# ---------------------------------------------------------------- pricing

def _pricing(config, net_id, layer):
    if config is None or config.pair_clearance_inert():
        c = config.clearance if config is not None else 0.1

        def pad_c(p, lyr=layer):
            return max(c, getattr(p, 'local_clearance', 0.0) or 0.0)

        def cu_c(other, kind, lyr=layer):
            return c
        return pad_c, cu_c

    def pad_c(p, lyr=layer):
        return config.pad_pair_clearance_before_override(
            p, net_id, lyr, base=config.clearance)

    def cu_c(other, kind, lyr=layer):
        return config.pair_clearance(net_id, other, lyr, kind=kind,
                                     base=config.clearance)
    return pad_c, cu_c


def _pad_on_layer(p, layer, board_copper):
    if getattr(p, 'drill', 0) and p.pad_type != 'np_thru_hole':
        return True
    from check_drc import pad_copper_layers
    return layer in pad_copper_layers(p, board_copper)


def _hole_xy(p):
    hx = p.hole_x if p.hole_x is not None else p.global_x
    hy = p.hole_y if p.hole_y is not None else p.global_y
    return hx, hy


def _near_pads(pcb_data, x0, y0, reach):
    for fp in pcb_data.footprints.values():
        for p in fp.pads:
            r = max(p.size_x, p.size_y) / 2.0 + reach
            if abs(p.global_x - x0) <= r and abs(p.global_y - y0) <= r:
                yield p


def leg_blockers(pcb_data, pts, layer, width, net_id, config,
                 own_pad=None) -> List[Blocker]:
    """Every foreign item the stub polyline `pts` (width `width`, on
    `layer`) would violate clearance to. Own-net copper is exempt."""
    from check_drc import segment_pad_gap
    hw = width / 2.0
    pad_c, cu_c = _pricing(config, net_id, layer)
    board_copper = list(getattr(pcb_data.board_info, 'copper_layers', None)
                        or getattr(config, 'layers', []) or [])
    hole_c = getattr(config, 'hole_clearance', 0.0) or 0.0
    out: List[Blocker] = []
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    span = max(max(xs) - min(xs), max(ys) - min(ys)) / 2.0 + hw + 0.6
    for p in _near_pads(pcb_data, cx, cy, span):
        if p is own_pad:
            continue
        drill = getattr(p, 'drill', 0) or 0.0
        if p.net_id != net_id and p.pad_type != 'np_thru_hole' \
                and _pad_on_layer(p, layer, board_copper):
            gap = min(segment_pad_gap(x1, y1, x2, y2, p)
                      for (x1, y1), (x2, y2) in zip(pts, pts[1:]))
            if gap < hw + pad_c(p) - 1e-6:
                out.append(Blocker('pad', p.net_id, p))
                continue
        if drill > 0 and hole_c > 0 and p.net_id != net_id:
            hx, hy = _hole_xy(p)
            d = min(_pt_seg_d(hx, hy, x1, y1, x2, y2)
                    for (x1, y1), (x2, y2) in zip(pts, pts[1:]))
            if d < drill / 2.0 + hw + hole_c - 1e-6:
                out.append(Blocker('drill', p.net_id, p))
    for s in pcb_data.segments:
        if s.net_id == net_id or s.layer != layer:
            continue
        lim = s.width / 2.0 + hw + cu_c(s.net_id, 'track')
        if (min(s.start_x, s.end_x) > max(xs) + lim
                or max(s.start_x, s.end_x) < min(xs) - lim
                or min(s.start_y, s.end_y) > max(ys) + lim
                or max(s.start_y, s.end_y) < min(ys) - lim):
            continue
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if _seg_seg_d(x1, y1, x2, y2, s.start_x, s.start_y,
                          s.end_x, s.end_y) < lim - 1e-6:
                out.append(Blocker('seg', s.net_id, s))
                break
    for v in pcb_data.vias:
        if v.net_id == net_id:
            continue
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            d = _pt_seg_d(v.x, v.y, x1, y1, x2, y2)
            if d < v.size / 2.0 + hw + cu_c(v.net_id, 'layer') - 1e-6 or (
                    hole_c > 0 and d < (v.drill or 0.0) / 2.0 + hw + hole_c
                    - 1e-6):
                out.append(Blocker('via', v.net_id, v))
                break
    return out


def via_blockers(pcb_data, x, y, size, drill, net_id, config,
                 extra_vias=()) -> List[Blocker]:
    """Every item a through via (size/drill) at (x, y) would violate a rule
    against: foreign copper on every layer, every drill at hole-to-hole
    (net-blind, #671), and foreign copper at copper-to-hole."""
    from check_drc import point_to_pad_distance
    vr, vd = size / 2.0, drill / 2.0
    h2h = getattr(config, 'hole_to_hole_clearance', 0.2) or 0.0
    hole_c = getattr(config, 'hole_clearance', 0.0) or 0.0
    out: List[Blocker] = []
    for v in list(pcb_data.vias) + list(extra_vias):
        d = math.hypot(x - v.x, y - v.y)
        if d > 3.0:
            continue
        if v.net_id != net_id and d < vr + v.size / 2.0 + \
                config.pair_clearance(net_id, v.net_id, kind='stack') - 1e-6:
            out.append(Blocker('via', v.net_id, v))
            continue
        if d < vd + (v.drill or 0.0) / 2.0 + h2h - 1e-6:
            # own-net or foreign: a drill too close is a fab rule. A foreign
            # via could move; an own-net one is ours to keep -> hard.
            out.append(Blocker('via' if v.net_id != net_id else 'drill',
                               v.net_id, v))
    for p in _near_pads(pcb_data, x, y, 1.5):
        drill_p = getattr(p, 'drill', 0) or 0.0
        if drill_p > 0:
            hx, hy = _hole_xy(p)
            if math.hypot(x - hx, y - hy) < vd + drill_p / 2.0 + h2h - 1e-6:
                out.append(Blocker('drill', p.net_id, p))
                continue
        if p.net_id == net_id or p.pad_type == 'np_thru_hole':
            continue
        lyr = next((l for l in p.layers if l.endswith('.Cu')), None)
        c = (config.clearance if config.pair_clearance_inert() else
             config.pad_pair_clearance_before_override(p, net_id))
        if math.hypot(x - p.global_x, y - p.global_y) - math.hypot(
                p.size_x, p.size_y) / 2.0 > vr + c + 1e-6 and not (
                hole_c > 0 and vd + hole_c > vr + c):
            continue
        dd = point_to_pad_distance(x, y, p)
        if dd < vr + c - 1e-6 or (hole_c > 0 and dd < vd + hole_c - 1e-6):
            out.append(Blocker('pad', p.net_id, p))
    for s in pcb_data.segments:
        if s.net_id == net_id:
            continue
        d = _pt_seg_d(x, y, s.start_x, s.start_y, s.end_x, s.end_y)
        if d < vr + s.width / 2.0 + config.pair_clearance(
                net_id, s.net_id, s.layer) - 1e-6 or (
                hole_c > 0 and d < vd + s.width / 2.0 + hole_c - 1e-6):
            out.append(Blocker('seg', s.net_id, s))
    return out


# ------------------------------------------------------------- candidates

def via_ladder(config, pcb_data, net_id):
    """(size, drill) pairs to try: the via this net draws, then each smaller
    rung of the escalation ladder -- the same ladder, raised to the same
    board/net rule floors, as the rescue's via step-down rungs
    (net_rescue._rescue_rungs)."""
    own = tuple(config.net_via(net_id)) if hasattr(config, 'net_via')         else (config.via_size, config.via_drill)
    out = [(own[0], own[1])]
    try:
        from fab_tiers import escalation_rungs
        n = len(pcb_data.board_info.copper_layers) or 2
        for r in escalation_rungs(n, extra_floors=config.rule_floors(net_id)):
            sz, dr = r['via_diameter'], r['via_drill']
            if sz < out[-1][0] - 1e-9:
                out.append((sz, dr))
    except Exception:                                    # noqa: BLE001
        pass
    return out


def width_ladder(config, pcb_data, net_id):
    """The net's nominal width, then the rescue's floor width
    (net_rescue._rescue_rungs: fab floor raised to the net's rule floor)."""
    layer = config.layers[0] if getattr(config, 'layers', None) else None
    nominal = (config.get_net_track_width(net_id, layer)
               if hasattr(config, 'get_net_track_width') else
               config.track_width)
    out = [nominal]
    try:
        from plane_pad_tap import fab_floor_clearance_track
        from fab_tiers import may_narrow
        if may_narrow():
            fl = min(nominal, fab_floor_clearance_track(pcb_data)[1],
                     (config.netclass_width_floors or {}).get(net_id, nominal))
            rf = config.rule_floors(net_id, layer).get('track_width')
            if rf:
                fl = min(nominal, max(fl, rf))
            if fl < nominal - 1e-9:
                out.append(fl)
    except Exception:                                    # noqa: BLE001
        pass
    return out


def _paths_to(dx, dy):
    """Octilinear paths from (0,0) to (dx, dy): straight when the offset is
    on one of the eight directions, else the two one-dogleg variants
    (axis leg then 45-degree leg, and 45-degree leg then axis leg)."""
    ax, ay = abs(dx), abs(dy)
    if ax < 1e-9 or ay < 1e-9 or abs(ax - ay) < 1e-9:
        return [[(0.0, 0.0), (dx, dy)]]
    sx, sy = math.copysign(1, dx), math.copysign(1, dy)
    m = min(ax, ay)
    diag = (sx * m, sy * m)
    if ax > ay:
        axis_first = (dx - sx * m, 0.0)
    else:
        axis_first = (0.0, dy - sy * m)
    return [[(0.0, 0.0), axis_first, (dx, dy)],
            [(0.0, 0.0), diag, (dx, dy)]]


def find_stub_via_sites(pcb_data, pad, net_id, config, *,
                        max_stub=MAX_STUB_MM, grid=None,
                        max_candidates=MAX_CANDIDATES,
                        layer=None) -> List[StubSite]:
    """Ranked stub+via escape candidates for `pad` (see module doc)."""
    if layer is None:
        cu = [l for l in pad.layers if l.endswith('.Cu')]
        if not cu:
            return []
        layer = cu[0]
    if grid is None:
        grid = getattr(config, 'grid_step', 0.1) or 0.1
    vias = via_ladder(config, pcb_data, net_id)
    widths = width_ladder(config, pcb_data, net_id)
    px, py = pad.global_x, pad.global_y
    full = pcb_data
    pcb_data = local_view(pcb_data, px, py, max_stub + 2.0)
    from check_drc import point_to_pad_distance
    n = int(round(max_stub / grid))
    offsets = sorted(((i * grid, j * grid) for i in range(-n, n + 1)
                      for j in range(-n, n + 1)
                      if math.hypot(i, j) * grid <= max_stub + 1e-9),
                     key=lambda d: math.hypot(*d))
    sites: List[StubSite] = []
    leg_memo = {}
    for size, drill in vias:
        for width in widths:
            first_free = None
            n_free = 0
            for dx, dy in offsets:
                r = math.hypot(dx, dy)
                # nearest-first: once enough free sites are in hand, the
                # rest are only longer stubs
                if n_free >= FREE_ENOUGH and r > first_free + FREE_SLACK_MM:
                    break
                vx, vy = px + dx, py + dy
                # the via must stand clear of its own pad's copper (a
                # via-in-pad is the #666 tap's business, not ours)
                if point_to_pad_distance(vx, vy, pad) < size / 2.0:
                    continue
                vb = via_blockers(pcb_data, vx, vy, size, drill,
                                  net_id, config)
                if any(b.hard for b in vb):
                    continue
                best = None
                for rel in _paths_to(dx, dy):
                    path = [(px + a, py + b) for a, b in rel]
                    if sum(math.hypot(q[0] - p_[0], q[1] - p_[1])
                           for p_, q in zip(path, path[1:]))                             > max_stub + 1e-9:
                        continue
                    lb = []
                    for a, b in zip(path, path[1:]):
                        k = (round(a[0], 4), round(a[1], 4),
                             round(b[0], 4), round(b[1], 4), width)
                        if k not in leg_memo:
                            leg_memo[k] = leg_blockers(
                                pcb_data, [a, b], layer, width, net_id,
                                config, own_pad=pad)
                        lb += leg_memo[k]
                    if any(b.hard for b in lb):
                        continue
                    st = StubSite(pad, layer, path, width, size, drill,
                                  _dedupe(vb + lb))
                    if best is None or st.rank_key() < best.rank_key():
                        best = st
                if best is not None:
                    sites.append(best)
                    if not best.blockers:
                        n_free += 1
                        if first_free is None:
                            first_free = r
            if n_free:
                break       # a free site at this via: no narrower track
        if any(not st.blockers for st in sites):
            break           # a free site at this via: no smaller via
    sites.sort(key=StubSite.rank_key)
    seen, uniq = set(), []
    for st in sites:
        k = (round(st.via_xy[0], 3), round(st.via_xy[1], 3), st.blocker_nets)
        if k not in seen:
            seen.add(k)
            uniq.append(st)
    return uniq[:max_candidates]


def local_view(pcb_data, x, y, reach):
    """The board's copper within `reach` of (x, y), with the attributes the
    blocker checks read -- a search walks thousands of sites, each against
    the few dozen items near the pad, not the whole board."""
    from types import SimpleNamespace

    def near_seg(s):
        return not (min(s.start_x, s.end_x) > x + reach
                    or max(s.start_x, s.end_x) < x - reach
                    or min(s.start_y, s.end_y) > y + reach
                    or max(s.start_y, s.end_y) < y - reach)
    pads = [p for fp in pcb_data.footprints.values() for p in fp.pads
            if abs(p.global_x - x) <= reach + max(p.size_x, p.size_y)
            and abs(p.global_y - y) <= reach + max(p.size_x, p.size_y)]
    return SimpleNamespace(
        segments=[s for s in pcb_data.segments if near_seg(s)],
        vias=[v for v in pcb_data.vias
              if abs(v.x - x) <= reach and abs(v.y - y) <= reach],
        footprints={'': SimpleNamespace(pads=pads)},
        board_info=pcb_data.board_info, nets=pcb_data.nets)


def _dedupe(blockers):
    seen = set()
    out = []
    for b in blockers:
        k = (b.kind, id(b.obj))
        if k not in seen:
            seen.add(k)
            out.append(b)
    return out


def site_copper(site: StubSite, net_id):
    """(segments, vias) kicad_parser objects for a site."""
    from kicad_parser import Segment, Via
    segs = [Segment(start_x=a[0], start_y=a[1], end_x=b[0], end_y=b[1],
                    width=site.width, layer=site.layer, net_id=net_id)
            for a, b in zip(site.path, site.path[1:])]
    x, y = site.via_xy
    vias = [Via(x=x, y=y, size=site.via_size, drill=site.via_drill,
                layers=['F.Cu', 'B.Cu'], net_id=net_id)]
    return segs, vias
