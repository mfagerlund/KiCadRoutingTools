"""Fine escape windows for grid-boxed pads.

A fine-pitch pad row can leave a legal exit band between its neighbours that
holds no line of the run's grid: on a 0.65 mm row at 0.25 mm track / 0.2 mm
clearance the band is 0.05 mm wide, and when it sits between two 0.1 mm grid
lines the main pass cannot leave the pad at all, however long it searches.
The A* spends its whole budget on the net and the net ends unrouted.

A pad is GRID-BOXED when the run grid's own raster -- every other-net pad
within reach, from any footprint, inflated by half width + clearance, plus
every other-net track and via on the board at that moment -- leaves no
8-connected path from the pad to a cell `reach` away, in any direction. For
such a pad an escape stub is routed at the rescue grid inside a small window
around the pad, on the pad's own layer, ending on a node of the run's grid
just past the row (outward, away from the footprint centre); the route then
continues from the stub's end at the run's grid.

The stub belongs to its net's routing. `fine_escape_prepass` runs before
the obstacle maps are built and reserves the stubs of the pads boxed on the
clean board: they go into pcb_data as copper of their (unrouted) net, so
every other net routes around the exit. Every routing site brackets its
route with `begin` / `finish`. `begin` adopts the net's reserved stubs, or
when there are none (the net was ripped, or routes again later) decides
boxedness against the copper present at that moment and lays fresh ones.
`finish` hands the stubs of a successful route to the front of its
new_segments: from there they are the net's routed copper, committed,
recorded and ripped with it. A failed route keeps a reservation and drops
fresh stubs. `release` takes the reservations of nets that never routed off
the board after the routing passes, so no stub ships unconnected.
"""
import math
from dataclasses import replace

import numpy as np

import routing_defaults as defaults

# Window reach past the footprint edge, in pitches.
WINDOW_PITCHES = 3
ELONGATED = 1.5  # long/short half-extent ratio that fixes a pad's exit axis

_STATE = '_fine_escape_state'


def _half_extents(pad):
    hx, hy = pad.size_x / 2, pad.size_y / 2
    rr = getattr(pad, 'rect_rotation', 0.0) or 0.0
    if rr:
        r = math.radians(rr)
        c, s = abs(math.cos(r)), abs(math.sin(r))
        hx, hy = hx * c + hy * s, hx * s + hy * c
    return hx, hy


def _on_layer(q, layer):
    return layer in q.layers or '*.Cu' in q.layers


def _pad_cells(q, margin, grid):
    from routing_utils import pad_blocked_cells_array
    gx, gy = round(q.global_x / grid), round(q.global_y / grid)
    hw, hh = q.size_x / 2, q.size_y / 2
    if q.shape in ('circle', 'oval'):
        cr = min(hw, hh)
    elif q.shape == 'roundrect':
        cr = getattr(q, 'roundrect_rratio', 0.25) * min(q.size_x, q.size_y)
    else:
        cr = 0
    return pad_blocked_cells_array(
        gx, gy, hw, hh, margin, grid, cr,
        off_x=q.global_x - gx * grid, off_y=q.global_y - gy * grid,
        rotation_deg=getattr(q, 'rect_rotation', 0.0) or 0.0)


def _all_pads(pcb_data):
    return [p for plist in pcb_data.pads_by_net.values() for p in plist]


def _raster_boxed(pcb_data, p, layer, margin, grid, reach, pads=None):
    """True when the run-grid raster leaves no 8-connected path from the
    pad's cells to `reach` mm away. Obstacles: every other-net pad on the
    layer within reach (the router's own conservative pad rasteriser,
    inflated by `margin` = half width + clearance) and every other-net track
    on the layer and via, inflated the same way."""
    r = int(math.ceil(reach / grid))
    cx0, cy0 = round(p.global_x / grid), round(p.global_y / grid)
    n = 2 * r + 1
    blocked = np.zeros((n, n), dtype=bool)

    def mark(cells, arr):
        if len(cells) == 0:
            return
        c = np.asarray(cells)
        i, j = c[:, 0] - cx0 + r, c[:, 1] - cy0 + r
        ok = (i >= 0) & (i < n) & (j >= 0) & (j < n)
        arr[i[ok], j[ok]] = True

    span = reach + 2
    for q in (pads if pads is not None else _all_pads(pcb_data)):
        if q is p or q.net_id == p.net_id or not _on_layer(q, layer):
            continue
        if abs(q.global_x - p.global_x) > span or abs(q.global_y - p.global_y) > span:
            continue
        mark(_pad_cells(q, margin, grid), blocked)

    xs = (cx0 - r + np.arange(n)) * grid
    ys = (cy0 - r + np.arange(n)) * grid
    X, Y = np.meshgrid(xs, ys, indexing='ij')
    x0, x1, y0, y1 = xs[0], xs[-1], ys[0], ys[-1]
    for s in pcb_data.segments:
        if s.net_id == p.net_id or s.layer != layer:
            continue
        lim = margin + s.width / 2
        if (max(s.start_x, s.end_x) < x0 - lim or min(s.start_x, s.end_x) > x1 + lim
                or max(s.start_y, s.end_y) < y0 - lim
                or min(s.start_y, s.end_y) > y1 + lim):
            continue
        dx, dy = s.end_x - s.start_x, s.end_y - s.start_y
        L2 = dx * dx + dy * dy
        if L2 > 0:
            t = np.clip(((X - s.start_x) * dx + (Y - s.start_y) * dy) / L2, 0, 1)
        else:
            t = 0.0
        d2 = (X - s.start_x - t * dx) ** 2 + (Y - s.start_y - t * dy) ** 2
        blocked |= d2 < lim * lim
    for v in pcb_data.vias:
        if v.net_id == p.net_id:
            continue
        lim = margin + v.size / 2
        if v.x < x0 - lim or v.x > x1 + lim or v.y < y0 - lim or v.y > y1 + lim:
            continue
        blocked |= (X - v.x) ** 2 + (Y - v.y) ** 2 < lim * lim

    own = np.zeros((n, n), dtype=bool)
    mark(_pad_cells(p, 0.0, grid), own)
    seeds = list(zip(*np.nonzero(own & ~blocked)))
    if not seeds:
        return True
    seen = blocked.copy()
    for c in seeds:
        seen[c] = True
    stack = seeds
    while stack:
        i, j = stack.pop()
        if i == 0 or j == 0 or i == n - 1 or j == n - 1:
            return False
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                a, b = i + di, j + dj
                if not seen[a, b]:
                    seen[a, b] = True
                    stack.append((a, b))
    return True


def _footprints(pcb_data):
    by_fp = {}
    for p in _all_pads(pcb_data):
        by_fp.setdefault(p.component_ref, []).append(p)
    return by_fp


def _bands(pcb_data, config, net_ids, by_fp=None):
    """Candidate pads of the nets in `net_ids`: the fine-pitch SMD pads of
    footprints with 4+ pads, with their outward exit model.

    Exit model: the track leaves the pad outward (away from its footprint's
    centroid, along the dominant axis) as a straight centreline at a
    perpendicular offset o. Legal o: inside the pad's own extent and at least
    hw + clearance from every other-net pad of the footprint whose span
    overlaps the corridor (pad centre to WINDOW_PITCHES pitches past the
    pad's outer edge). Returns dicts (pad, horiz, sgn, lo, hi, pitch, end):
    `end` is the corridor's far edge along the exit axis past every
    neighbour that bounds it; the stub's targets lie just past it."""
    wanted = set(net_ids)
    by_fp = by_fp if by_fp is not None else _footprints(pcb_data)
    refs = sorted({p.component_ref for nid in wanted
                   for p in pcb_data.pads_by_net.get(nid, [])})
    out = []
    for ref in refs:
        pads = by_fp.get(ref, [])
        if len(pads) < 4:
            continue
        cx = sum(p.global_x for p in pads) / len(pads)
        cy = sum(p.global_y for p in pads) / len(pads)
        for p in sorted(pads, key=lambda q: str(q.pad_number)):
            if p.net_id not in wanted or getattr(p, 'drill', 0) > 0:
                continue
            layer = next((l for l in p.layers if l in config.layers), None)
            if layer is None:
                continue
            hw = config.get_net_track_width(p.net_id, layer) / 2
            clr = config.get_net_clearance(p.net_id)
            dx, dy = p.global_x - cx, p.global_y - cy
            phx, phy = _half_extents(p)
            # An elongated row pad exits along its long axis; the centroid
            # direction only decides square pads (a corner pad of a QFN is
            # nearly diagonal from the centroid).
            if phx > ELONGATED * phy:
                horiz = True
            elif phy > ELONGATED * phx:
                horiz = False
            else:
                horiz = abs(dx) >= abs(dy)
            sgn = 1 if (dx if horiz else dy) >= 0 else -1
            a0 = p.global_x if horiz else p.global_y
            pc = p.global_y if horiz else p.global_x
            half_a, half_p = (phx, phy) if horiz else (phy, phx)
            near = []
            for q in pads:
                if q is p or q.net_id == p.net_id or not _on_layer(q, layer):
                    continue
                qhx, qhy = _half_extents(q)
                near.append((q.global_x if horiz else q.global_y,
                             q.global_y if horiz else q.global_x,
                             qhx if horiz else qhy, qhy if horiz else qhx))
            offs = [abs(qp - pc) for _, qp, _, _ in near if abs(qp - pc) > 1e-6]
            if not offs:
                continue
            pitch = min(offs)
            if pitch > 2 * (2 * hw + 2 * clr) + 2 * half_p:
                continue  # coarse pitch: never grid-limited
            lo, hi = pc - half_p, pc + half_p
            c0, c1 = sorted((a0, a0 + sgn * (half_a + WINDOW_PITCHES * pitch)))
            end = a0 + sgn * half_a
            for qa, qp, qha, qhp in near:
                if qa + qha + clr + hw < c0 or qa - qha - clr - hw > c1:
                    continue
                if qp >= pc:
                    hi = min(hi, qp - qhp - clr - hw)
                else:
                    lo = max(lo, qp + qhp + clr + hw)
                far = qa + sgn * qha
                end = max(end, far) if sgn > 0 else min(end, far)
            if lo > hi + 1e-9:
                continue  # no legal band at all: a stub cannot pass either
            out.append(dict(pad=p, layer=layer, horiz=horiz, sgn=sgn, lo=lo,
                            hi=hi, pitch=pitch, end=end, hw=hw, clr=clr,
                            reach=half_a + WINDOW_PITCHES * pitch))
    return out


def _boxed(pcb_data, b, grid, pads=None):
    return _raster_boxed(pcb_data, b['pad'], b['layer'], b['hw'] + b['clr'],
                         grid, b['reach'], pads)


def find_grid_boxed_pads(pcb_data, config, net_ids):
    """Grid-boxed pads of the nets in `net_ids` against the board as it is
    now (pads of every footprint, and the copper in pcb_data)."""
    pads = _all_pads(pcb_data)
    return [b for b in _bands(pcb_data, config, net_ids)
            if _boxed(pcb_data, b, config.grid_step, pads)]


def _has_own_copper(pcb_data, pad):
    hx, hy = _half_extents(pad)
    for s in pcb_data.segments:
        if s.net_id != pad.net_id:
            continue
        for x, y in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
            if abs(x - pad.global_x) <= hx + 1e-6 and abs(y - pad.global_y) <= hy + 1e-6:
                return True
    return False


def _targets(b, grid):
    """Run-grid nodes just past the row: one along-coordinate beyond the
    bounding neighbours' far edge by clearance + half width + one grid step
    (the run's obstacle maps expand conservatively), on the two run-grid
    lines nearest the pad centre."""
    p = b['pad']
    pc = p.global_y if b['horiz'] else p.global_x
    reach = b['end'] + b['sgn'] * (b['clr'] + b['hw'] + grid)
    k = math.ceil(reach / grid - 1e-9) if b['sgn'] > 0 else math.floor(reach / grid + 1e-9)
    along = [k * grid, (k + b['sgn']) * grid]
    perp = [math.floor(pc / grid) * grid, math.ceil(pc / grid) * grid]
    pts = []
    for a in along:
        for o in sorted(set(round(v, 6) for v in perp)):
            x, y = (a, o) if b['horiz'] else (o, a)
            pts.append((round(x, 6), round(y, 6), b['layer']))
    return pts


def _route_stub(pcb_data, b, config, net_clearances):
    from net_rescue import _pristine_rescue_map, _fence_window, seed_rows
    from net_rescue import _result_escapes_window
    from plane_pad_tap import make_local_window
    from routing_config import GridCoord
    from single_ended_routing import route_net_with_obstacles

    p = b['pad']
    tgt_pts = _targets(b, config.grid_step)
    xs = [p.global_x] + [t[0] for t in tgt_pts]
    ys = [p.global_y] + [t[1] for t in tgt_pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    half = max(max(xs) - min(xs), max(ys) - min(ys)) / 2 + 2 * b['pitch']
    cfg = replace(config, grid_step=defaults.RESCUE_GRID_STEP,
                  layers=[b['layer']], bga_exclusion_zones=[])
    window = make_local_window(pcb_data, cx, cy, half)
    obstacles = _pristine_rescue_map(window, pcb_data, cfg, p.net_id,
                                     net_clearances, ('fine-escape', cx, cy, half))
    _fence_window(obstacles, window, cfg)
    src = seed_rows([(p.global_x, p.global_y, b['layer'])], cfg, window)
    tgt = seed_rows(tgt_pts, cfg, window)
    if not src or not tgt:
        return None
    bounds = None
    if window.board_info.board_bounds:
        wc = GridCoord(cfg.grid_step)
        x0, y0, x1, y1 = window.board_info.board_bounds
        g0, g1 = wc.to_grid(x0, y0), wc.to_grid(x1, y1)
        bounds = (g0[0], g0[1], g1[0], g1[1])
    result = route_net_with_obstacles(window, p.net_id, cfg, obstacles,
                                      bounds=bounds, sources_override=src,
                                      targets_override=tgt)
    if not result or result.get('failed') or result.get('new_vias'):
        return None
    if _result_escapes_window(result, window, cfg):
        return None
    return result


class _State:
    def __init__(self, nets, by_fp, pads, net_clearances, verbose):
        self.nets = nets
        self.by_fp = by_fp
        self.pads = pads
        self.net_clearances = net_clearances
        self.verbose = verbose
        self.pending = {}   # net id -> reserved stub segments in pcb_data
        self.live = None    # (net id, segs, adopted) of the open bracket
        self.laid = 0       # stub segments laid over the run
        self.kept = 0       # stub segments handed to a successful route
        self.dogbone = set()  # nets whose reservations escape_reserve plans
        self.plane = set()    # nets whose dog-bone reaches a pour: kept at release
        self.maps = None      # routing state holding the persistent maps
        self.config = None
        self.failed = set()   # dog-bone nets that failed: released at the next begin
        self.dropped = 0      # dog-bone objects a successful route did not use


def _lay(pcb_data, st, net_id, config):
    if net_id in st.dogbone:
        # A dog-bone is planned once, jointly, before the maps exist. A fresh
        # lay later sees only pcb_data, not copper that so far lives only in
        # the obstacle maps (phase-3 taps), and shorts it: never lay fresh.
        return []
    return _lay_stubs(pcb_data, st, net_id, config)


def attach_routing_state(pcb_data, state, config):
    """Give the bracket the run's persistent obstacle maps (`state`'s
    working_obstacles / net_obstacles_cache), so a dog-bone net that fails
    can have its reservation released while routing goes on."""
    st = getattr(pcb_data, _STATE, None)
    if st is not None:
        st.maps = state
        st.config = config


def _flush_failed(pcb_data, st, keep=None):
    """Release the reservations of dog-bone nets that failed since the last
    bracket (except `keep`, which is about to route again and adopts its
    own). Runs at a bracket's start, when every other net's entry is back in
    the working map, so the refresh is balanced."""
    nids = sorted(n for n in st.failed if n != keep and n in st.pending
                  and n not in st.plane)
    st.failed = {n for n in st.failed if n == keep}
    if not nids or st.maps is None:
        return
    for nid in nids:
        _drop(pcb_data, st.pending.pop(nid))
    from obstacle_cache import refresh_net_obstacles
    refresh_net_obstacles(getattr(st.maps, 'working_obstacles', None),
                          getattr(st.maps, 'net_obstacles_cache', None),
                          pcb_data, st.config, nids)
    if st.verbose:
        print(f"  fine escape: released the reservations of {len(nids)} failed net(s)")


def _lay_stubs(pcb_data, st, net_id, config):
    """Route the stubs of `net_id`'s boxed pads against the copper present
    now and append them to pcb_data as the net's copper. Returns them."""
    from kicad_parser import Segment
    from pcb_modification import bump_copper_epoch
    segs = []
    for b in _bands(pcb_data, config, [net_id], st.by_fp):
        p = b['pad']
        if _has_own_copper(pcb_data, p) or not _boxed(pcb_data, b, config.grid_step,
                                                      st.pads):
            continue
        result = _route_stub(pcb_data, b, config, st.net_clearances)
        if result is None:
            if st.verbose:
                print(f"  fine escape: {p.component_ref}.{p.pad_number} "
                      f"({p.net_name}) not routed")
            continue
        for s in result.get('new_segments') or []:
            seg = Segment(start_x=s.start_x, start_y=s.start_y, end_x=s.end_x,
                          end_y=s.end_y, width=s.width, layer=s.layer,
                          net_id=net_id)
            pcb_data.segments.append(seg)
            segs.append(seg)
    if segs:
        bump_copper_epoch(pcb_data)
        st.laid += len(segs)
    return segs


def fine_escape_prepass(pcb_data, config, net_ids, net_clearances=None,
                        verbose=False):
    """Arm fine escape for `net_ids` and reserve the stubs of the pads that
    are boxed on the board as it is before routing. Call before the obstacle
    maps are built: a reserved stub is copper of an unrouted net, so every
    other net's route keeps off it. Returns the number of nets reserved."""
    by_fp = _footprints(pcb_data)
    cands = _bands(pcb_data, config, net_ids, by_fp)
    nets = {b['pad'].net_id for b in cands}
    st = _State(nets, by_fp, _all_pads(pcb_data), net_clearances, verbose)
    setattr(pcb_data, _STATE, st)
    for nid in sorted(nets):
        segs = _lay(pcb_data, st, nid, config)
        if segs:
            st.pending[nid] = segs
    print(f"Fine escape: {len(cands)} candidate pad(s) on {len(nets)} net(s); "
          f"{st.laid} stub segment(s) reserved on {len(st.pending)} net(s)")
    return len(st.pending)


def _drop(pcb_data, segs):
    """Take reserved copper (segments and vias) out of pcb_data."""
    from pcb_modification import bump_copper_epoch
    if not segs:
        return
    ids = {id(s) for s in segs}
    pcb_data.segments[:] = [s for s in pcb_data.segments if id(s) not in ids]
    pcb_data.vias[:] = [v for v in pcb_data.vias if id(v) not in ids]
    bump_copper_epoch(pcb_data)


def _is_via(o):
    return hasattr(o, 'drill') and not hasattr(o, 'start_x')


def begin(pcb_data, net_id, config):
    """Open the bracket around one route of `net_id`: adopt its reserved
    stubs, or lay fresh ones against the copper present now. The stubs sit
    in pcb_data as the net's copper while it routes. Returns a token for
    `finish`."""
    st = getattr(pcb_data, _STATE, None)
    if st is None:
        return None
    if st.live is not None:  # a bracket an exception left open
        nid, segs, adopted = st.live
        if adopted:
            st.pending[nid] = segs
        else:
            _drop(pcb_data, segs)
        st.live = None
    _flush_failed(pcb_data, st, keep=net_id)
    if net_id not in st.nets:
        return None
    if net_id in st.pending:
        st.live = (net_id, st.pending.pop(net_id), True)
    else:
        segs = _lay(pcb_data, st, net_id, config)
        if not segs:
            return None
        st.live = (net_id, segs, False)
    if st.verbose:
        print(f"  fine escape: {len(st.live[1])} stub segment(s) "
              f"{'adopted' if st.live[2] else 'laid'}")
    return st.live


def finish(pcb_data, token, result):
    """Close the bracket. A successful route takes the stubs: out of
    pcb_data and to the front of its new_segments, so they are committed,
    recorded and ripped with the route. A failed route leaves reserved stubs
    reserved (the obstacle maps already hold them) and drops fresh ones (the
    maps never saw them). Returns `result`."""
    st = getattr(pcb_data, _STATE, None)
    if token is None or st is None or st.live is not token:
        return result
    st.live = None
    net_id, segs, adopted = token
    if result and not result.get('failed'):
        _drop(pcb_data, segs)
        if net_id in st.dogbone and net_id not in st.plane:
            used = _used_bones(pcb_data, net_id, segs, result)
            st.dropped += len(segs) - len(used)
            segs = used
        vias = [o for o in segs if _is_via(o)]
        tracks = [o for o in segs if not _is_via(o)]
        result['new_segments'] = tracks + list(result.get('new_segments') or [])
        if vias:
            result['new_vias'] = vias + list(result.get('new_vias') or [])
        st.kept += len(segs)
    elif adopted:
        st.pending[net_id] = segs
        if net_id in st.dogbone:
            st.failed.add(net_id)
    else:
        _drop(pcb_data, segs)
    return result


def _bones(objs):
    """Split a dog-bone net's reserved objects into bones: each via with
    the stub segments laid just before it (escape_reserve._copper order)."""
    out, cur = [], []
    for o in objs:
        cur.append(o)
        if _is_via(o):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _used_bones(pcb_data, net_id, objs, result):
    """The bones a successful route needs: those whose via or stub its new
    copper reaches, and those of pads it never touched (a later tap may land
    there). A bone beside a pad the route reached its own way is dropped, so
    it does not ship as dead copper that blocked the neighbours."""
    segs = list(result.get('new_segments') or [])
    vias = list(result.get('new_vias') or [])
    pts = [(s.start_x, s.start_y) for s in segs] + [(s.end_x, s.end_y) for s in segs]
    pts += [(v.x, v.y) for v in vias]
    pads = list(pcb_data.pads_by_net.get(net_id, []))
    keep = []
    for bone in _bones(objs):
        via = next((o for o in bone if _is_via(o)), None)
        tracks = [o for o in bone if not _is_via(o)]
        start = ((tracks[0].start_x, tracks[0].start_y) if tracks
                 else (via.x, via.y) if via else None)
        pad = min(pads, key=lambda p: math.hypot(p.global_x - start[0],
                                                 p.global_y - start[1]),
                  default=None) if start else None

        def in_pad(x, y):
            if pad is None:
                return False
            hx, hy = _half_extents(pad)
            return abs(x - pad.global_x) <= hx + 1e-6 and abs(y - pad.global_y) <= hy + 1e-6

        def on_bone(x, y):
            if via is not None and math.hypot(x - via.x, y - via.y) <= via.size / 2 + 1e-6:
                return True
            for t in tracks:
                if _pt_seg_d(x, y, t.start_x, t.start_y, t.end_x, t.end_y) <= t.width / 2 + 1e-6:
                    return True
            return False

        reached = any(on_bone(x, y) and not in_pad(x, y) for x, y in pts)
        touched = any(in_pad(x, y) for x, y in pts)
        if reached or not touched:
            keep.extend(bone)
    return keep


def _pt_seg_d(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L <= 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def release(pcb_data, working_obstacles=None, net_obstacles_cache=None,
            config=None):
    """End of the routing passes: take the reservations of nets that never
    routed off the board, so no stub ships as unconnected copper and later
    passes see the pads bare. Returns the number of nets released."""
    st = getattr(pcb_data, _STATE, None)
    if st is None:
        return 0
    # Nothing is pending after this, so the bound routing state is never read
    # again: drop it, or its obstacle maps outlive the pass into the pour
    # refill that follows (one 6-layer board: +2.7 GB peak, killed at the memory cap).
    st.maps = None
    st.failed = set()
    if not st.pending:
        return 0
    # A dog-bone of a net with a pour is the pin's plane connection: keep it.
    planes = sorted(n for n in st.pending if n in st.plane)
    for nid in planes:
        st.kept += len(st.pending.pop(nid))
    nids = sorted(st.pending)
    for nid in nids:
        _drop(pcb_data, st.pending.pop(nid))
    if config is not None:
        from obstacle_cache import refresh_net_obstacles
        refresh_net_obstacles(working_obstacles, net_obstacles_cache, pcb_data,
                              config, nids)
    print(f"Fine escape: {st.kept} stub segment(s) kept by routes; "
          f"reservations of {len(nids)} unrouted net(s) released"
          + (f", {len(planes)} plane net(s) kept" if planes else "")
          + (f", {st.dropped} unused dog-bone object(s) dropped" if st.dropped else ""))
    return len(nids)
