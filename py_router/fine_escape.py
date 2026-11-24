"""Fine escape windows for grid-boxed pads.

A fine-pitch pad row can leave a legal exit band between its neighbours that
holds no line of the run's grid: on a 0.65 mm row at 0.25 mm track / 0.2 mm
clearance the band is 0.05 mm wide, and when it sits between two 0.1 mm grid
lines the main pass cannot leave the pad at all, however long it searches.
The A* spends its whole budget on the net and the net ends unrouted.

Before the main pass, this pre-pass finds such GRID-BOXED pads (the band is
non-empty, but no multiple of grid_step falls in it), routes only the escape
stub of each one at the rescue grid inside a small window around the pad, on
the pad's own layer, ending on a node of the run's grid just past the row,
and commits the stub as the net's own copper. The main pass then routes from
the stub's end at the run's grid, unchanged.

Stubs are routed in a fixed order (by footprint, then pad), each against the
copper of the stubs before it. A stub that does not route leaves the pad as
it was: the main pass then sees exactly what it saw without this pass.
"""
import math
from dataclasses import replace

import routing_defaults as defaults

# Window reach past the footprint edge, in pitches.
WINDOW_PITCHES = 3


def _half_extents(pad):
    hx, hy = pad.size_x / 2, pad.size_y / 2
    rr = getattr(pad, 'rect_rotation', 0.0) or 0.0
    if rr:
        r = math.radians(rr)
        c, s = abs(math.cos(r)), abs(math.sin(r))
        hx, hy = hx * c + hy * s, hx * s + hy * c
    return hx, hy


def _has_line(lo, hi, step):
    k = math.ceil(lo / step - 1e-9)
    return k * step <= hi + 1e-9


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


def _raster_boxed(p, pads, layer, margin, grid, reach):
    """True when the run-grid raster (the router's own conservative pad
    rasteriser, other-net pads of the footprint inflated by hw + clearance)
    leaves no 8-connected path from the pad's centre cell to `reach` mm away:
    a grid line sits in the legal band but the raster blocks it."""
    blocked = set()
    for q in pads:
        if q is p or q.net_id == p.net_id or (layer not in q.layers
                                              and '*.Cu' not in q.layers):
            continue
        if abs(q.global_x - p.global_x) > reach + 2 or                 abs(q.global_y - p.global_y) > reach + 2:
            continue
        blocked.update(map(tuple, _pad_cells(q, margin, grid).tolist()))
    c0 = (round(p.global_x / grid), round(p.global_y / grid))
    seeds = [c for c in map(tuple, _pad_cells(p, 0.0, grid).tolist())
             if c not in blocked]
    if not seeds:
        return True
    r = int(math.ceil(reach / grid))
    seen, stack = set(seeds), list(seeds)
    while stack:
        x, y = stack.pop()
        if abs(x - c0[0]) >= r or abs(y - c0[1]) >= r:
            return False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                n = (x + dx, y + dy)
                if n not in seen and n not in blocked:
                    seen.add(n)
                    stack.append(n)
    return True


def find_grid_boxed_pads(pcb_data, config, net_ids):
    """Grid-boxed pads of the nets in `net_ids`.

    Exit model: the track leaves the pad outward (away from its footprint's
    centroid, along the dominant axis) as a straight centreline at a
    perpendicular offset o. Legal o: inside the pad's own extent and at least
    hw + clearance from every other-net pad of the footprint whose span
    overlaps the corridor (pad centre to WINDOW_PITCHES pitches past the
    pad's outer edge). Returns dicts (pad, horiz, sgn, lo, hi, pitch, end):
    `end` is the corridor's far edge along the exit axis past every
    neighbour that bounds it."""
    wanted = set(net_ids)
    by_fp = {}
    for plist in pcb_data.pads_by_net.values():
        for p in plist:
            by_fp.setdefault(p.component_ref, []).append(p)
    grid = config.grid_step
    out = []
    for ref in sorted(by_fp):
        pads = by_fp[ref]
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
            horiz = abs(dx) >= abs(dy)
            sgn = 1 if (dx if horiz else dy) >= 0 else -1
            phx, phy = _half_extents(p)
            a0 = p.global_x if horiz else p.global_y
            pc = p.global_y if horiz else p.global_x
            half_a, half_p = (phx, phy) if horiz else (phy, phx)
            near = []
            for q in pads:
                if q is p or q.net_id == p.net_id or layer not in q.layers \
                        and '*.Cu' not in q.layers:
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
                continue
            if _has_line(lo, hi, grid) and not _raster_boxed(
                    p, pads, layer, hw + clr, grid,
                    half_a + WINDOW_PITCHES * pitch):
                continue
            out.append(dict(pad=p, layer=layer, horiz=horiz, sgn=sgn, lo=lo,
                            hi=hi, pitch=pitch, end=end, hw=hw, clr=clr))
    return out


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


def _route_stub(pcb_data, b, config, net_clearances, grid=None):
    """The escape stub of one pad inside its window, at `grid` (the rescue
    grid by default). None when it does not route."""
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
    grid = grid or defaults.RESCUE_GRID_STEP
    cfg = replace(config, grid_step=grid,
                  layers=[b['layer']], bga_exclusion_zones=[])
    window = make_local_window(pcb_data, cx, cy, half)
    obstacles = _pristine_rescue_map(window, pcb_data, cfg, p.net_id,
                                     net_clearances,
                                     ('fine-escape', cx, cy, half, grid))
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


def fine_escape_prepass(pcb_data, config, net_ids, all_swap_segments,
                        net_clearances=None, verbose=False):
    """Route and commit the escape stub of every grid-boxed pad of `net_ids`.
    Returns (boxed, placed)."""
    from kicad_parser import Segment
    from pcb_modification import bump_copper_epoch

    boxed = find_grid_boxed_pads(pcb_data, config, net_ids)
    placed = escapable = 0
    for b in boxed:
        p = b['pad']
        if _has_own_copper(pcb_data, p):
            continue
        # The geometric test is conservative: the main pass has more ways out
        # of a pad than a straight exit. A pad the run's own grid escapes in
        # the same window is not boxed and gets no stub: a stub there only
        # pins the exit and costs the main pass search.
        if _route_stub(pcb_data, b, config, net_clearances,
                       grid=config.grid_step) is not None:
            escapable += 1
            continue
        result = _route_stub(pcb_data, b, config, net_clearances)
        if result is None:
            if verbose:
                print(f"  fine escape: {p.component_ref}.{p.pad_number} "
                      f"({p.net_name}) not routed")
            continue
        for s in result.get('new_segments') or []:
            seg = Segment(start_x=s.start_x, start_y=s.start_y, end_x=s.end_x,
                          end_y=s.end_y, width=s.width, layer=s.layer,
                          net_id=p.net_id)
            pcb_data.segments.append(seg)
            all_swap_segments.append(seg)
        bump_copper_epoch(pcb_data)
        placed += 1
    print(f"Fine escape: {len(boxed) - escapable} grid-boxed pad(s), {placed} stub(s) placed")
    return len(boxed) - escapable, placed
