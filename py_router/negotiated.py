"""Negotiated-congestion main pass (PathFinder) for single-ended nets.

KRT_NEGOTIATED=1 (environment) makes route.py route its single-ended nets here first: every net is routed as a
complete Steiner tree by grid_router.NegotiatedRouter, in parallel, with overlaps priced and renegotiated until no
two nets share copper (see rust_router/src/pathfinder.rs). The nets it legalises are registered exactly as the
sequential loop registers a success (pcb_data, routed results, obstacle caches), so every later KRT stage (reroute
loop, Phase 3, rescue, cleanup, write, regrade) runs unchanged. Nets it cannot route legally return to the
sequential loop (route_single_ended_nets), which then routes them with its full machinery around the negotiated
copper.

Knobs (environment): KRT_NEGOTIATED_THREADS (default: all cores), KRT_NEGOTIATED_PASSES (60),
KRT_NEGOTIATED_SECONDS (time limit of the negotiation, default 600; a stop file stops it too),
KRT_NEGOTIATED_MARGIN_MM (search window margin around a net's terminals, 6 mm).
"""
import os
import time
from typing import Dict, List, Tuple

import numpy as np

import grid_router
from connectivity import get_multipoint_net_pads, get_net_endpoints, calculate_stub_length
from geometry_utils import simplify_path
from kicad_parser import Segment, Via
from net_queries import calculate_route_length, expand_pad_layers
from obstacle_cache import (_cache_cells_and_vias, add_net_obstacles_from_cache,
                            remove_net_obstacles_from_cache, update_net_obstacles_after_routing)
from obstacle_costs import compute_track_proximity_for_net
from obstacle_map import get_same_net_through_hole_positions
from pcb_modification import add_route_to_pcb_data
from routing_config import GridCoord
from routing_state import record_net_event
from routing_utils import build_layer_map


def enabled() -> bool:
    return os.environ.get('KRT_NEGOTIATED', '') not in ('', '0')


def _env_num(name, default, kind=float):
    v = os.environ.get(name)
    return kind(v) if v not in (None, '') else default


def pad_terminal_cells(pad, coord, layer_map, routing_layers, track_width):
    """A pad's terminal cells: its centre cell first, then the cells of the disc inscribed in the pad (less half a
    track) on every routing layer the pad has. Rust keeps the centre and drops the other cells that are blocked."""
    gx, gy = coord.to_grid(pad.global_x, pad.global_y)
    r = max(0.0, (min(pad.size_x, pad.size_y) / 2.0 - track_width / 2.0) / coord.grid_step)
    ri = int(r)
    offs = [(0, 0)] + [(dx, dy) for dy in range(-ri, ri + 1) for dx in range(-ri, ri + 1)
                       if (dx or dy) and dx * dx + dy * dy <= r * r]
    layers = [layer_map[l] for l in expand_pad_layers(pad.layers, routing_layers) if l in layer_map]
    return [(gx + dx, gy + dy, li) for li in layers for dx, dy in offs], (pad.global_x, pad.global_y)


def net_terminals(pcb_data, net_id, config, coord, layer_map):
    """[(cells, {cell: (x, y) original point})] per terminal, or None when the net has no two terminals."""
    tw = config.track_width
    mp = get_multipoint_net_pads(pcb_data, net_id, config)
    out = []
    if mp:
        for e in mp:
            obj = e[5] if len(e) > 5 else None
            if obj is not None and hasattr(obj, 'global_x') and hasattr(obj, 'size_x'):
                cells, orig = pad_terminal_cells(obj, coord, layer_map, config.layers, tw)
                out.append((cells, {c: orig for c in cells[:len(cells)]}))
            else:
                c = (e[0], e[1], e[2])
                out.append(([c], {c: (e[3], e[4])}))
    else:
        sources, targets, err = get_net_endpoints(pcb_data, net_id, config)
        if not sources or not targets:
            return None
        for side in (sources, targets):
            cells = [(s[0], s[1], s[2]) for s in side]
            out.append((cells, {(s[0], s[1], s[2]): (s[3], s[4]) for s in side}))
    out = [t for t in out if t[0]]
    return out if len(out) >= 2 else None


def path_copper(pcb_data, net_id, config, path, orig_of, layer_names, coord, th_positions):
    """Segments and vias of one grid path (KRT's own conversion: original-point stubs, collinear merge, vias at
    layer changes except at the net's through-hole pads)."""
    from single_ended_routing import _emit_via_size
    segs, vias = [], []
    p0, p1 = path[0], path[-1]
    simp = simplify_path(path)

    def stub(cell, at_start):
        o = orig_of.get(tuple(cell))
        if o is None:
            return
        gx, gy = coord.to_float(cell[0], cell[1])
        if abs(o[0] - gx) > 0.001 or abs(o[1] - gy) > 0.001:
            ln = layer_names[cell[2]]
            a, b = ((o[0], o[1]), (gx, gy)) if at_start else ((gx, gy), (o[0], o[1]))
            segs.append(Segment(start_x=a[0], start_y=a[1], end_x=b[0], end_y=b[1],
                                width=config.get_net_track_width(net_id, ln), layer=ln, net_id=net_id))
    stub(p0, True)
    for i in range(len(simp) - 1):
        gx1, gy1, l1 = simp[i]
        gx2, gy2, l2 = simp[i + 1]
        x1, y1 = coord.to_float(gx1, gy1)
        x2, y2 = coord.to_float(gx2, gy2)
        if l1 != l2:
            if (gx1, gy1) not in th_positions:
                vsz, vdr = _emit_via_size(pcb_data, gx1, gy1, config, net_id=net_id, x=x1, y=y1)
                vias.append(Via(x=x1, y=y1, size=vsz, drill=vdr, layers=["F.Cu", "B.Cu"], net_id=net_id))
        elif (x1, y1) != (x2, y2):
            ln = layer_names[l1]
            segs.append(Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2,
                                width=config.get_net_track_width(net_id, ln), layer=ln, net_id=net_id))
    stub(p1, False)
    return segs, vias


def grid_bounds(pcb_data, coord, margin_cells=4):
    bb = getattr(pcb_data.board_info, 'board_bounds', None)
    xs, ys = [], []
    if bb:
        xs += [bb[0], bb[2]]
        ys += [bb[1], bb[3]]
    for pads in pcb_data.pads_by_net.values():
        for p in pads:
            xs.append(p.global_x)
            ys.append(p.global_y)
    gx0, gy0 = coord.to_grid(min(xs), min(ys))
    gx1, gy1 = coord.to_grid(max(xs), max(ys))
    return gx0 - margin_cells, gy0 - margin_cells, gx1 - gx0 + 1 + 2 * margin_cells, gy1 - gy0 + 1 + 2 * margin_cells


def register_success(state, net_id, result):
    """What route_single_ended_nets does for a routed net, minus bus bookkeeping."""
    pcb_data, config = state.pcb_data, state.config
    stub_length = calculate_stub_length(pcb_data, net_id)
    result['route_length'] = calculate_route_length(result['new_segments'], result.get('new_vias', []), pcb_data) + stub_length
    result['stub_length'] = stub_length
    state.results.append(result)
    add_route_to_pcb_data(pcb_data, result, debug_lines=config.debug_lines)
    from plane_fragility import fragility_on_copper_change
    fragility_on_copper_change(config, pcb_data, result.get('new_segments'), result.get('new_vias'))
    if net_id in state.remaining_net_ids:
        state.remaining_net_ids.remove(net_id)
    state.routed_net_ids.append(net_id)
    state.routed_results[net_id] = result
    if result.get('path'):
        state.routed_net_paths[net_id] = result['path']
    record_net_event(state, net_id, "initial_route", {
        "type": "negotiated", "segments": len(result['new_segments']),
        "vias": len(result.get('new_vias', [])), "iterations": result['iterations']})
    state.track_proximity_cache[net_id] = compute_track_proximity_for_net(pcb_data, net_id, config, state.layer_map)
    cache = state.net_obstacles_cache
    if state.working_obstacles is not None and cache is not None:
        old = cache.get(net_id)
        if old is not None:
            remove_net_obstacles_from_cache(state.working_obstacles, old)
        update_net_obstacles_after_routing(pcb_data, net_id, result, config, cache)
        add_net_obstacles_from_cache(state.working_obstacles, cache[net_id])


def route_negotiated(state, single_ended_nets: List[Tuple[str, int]], cancel_check=None):
    """Route single_ended_nets by negotiated congestion. Returns (successful, rest, seconds): rest are the
    (name, id) the sequential loop still has to route."""
    t0 = time.time()
    pcb_data, config = state.pcb_data, state.config
    coord = GridCoord(config.grid_step)
    layer_map = build_layer_map(config.layers)
    layer_names = list(config.layers)
    g = config.grid_step
    olinks = getattr(state, 'oracle_links_by_net', None) or {}

    x0, y0, w, h = grid_bounds(pcb_data, coord)
    router = grid_router.NegotiatedRouter(state.base_obstacles, x0, y0, w, h)
    for nid, cd in (state.net_obstacles_cache or {}).items():
        cells, vias = _cache_cells_and_vias(cd)
        router.add_keepout(int(nid), np.ascontiguousarray(cells, dtype=np.int32),
                           np.ascontiguousarray(vias, dtype=np.int32))

    picked, rest, origs = [], [], []
    for name, nid in single_ended_nets:
        if nid in olinks:
            rest.append((name, nid))
            continue
        terms = net_terminals(pcb_data, nid, config, coord, layer_map)
        if not terms:
            rest.append((name, nid))
            continue
        router.add_net(int(nid), int(nid), [t[0] for t in terms])
        orig = {}
        for _, o in terms:
            orig.update(o)
        picked.append((name, nid))
        origs.append(orig)

    tw, cl = config.track_width, config.clearance
    vd, dr = config.via_size, config.via_drill
    h2h = getattr(config, 'hole_to_hole_clearance', 0.0) or 0.0
    m = 0.25
    stop = os.environ.get('KICAD_STOP_FILE') or os.environ.get('ROUTE_STOP_FILE')
    seconds = _env_num('KRT_NEGOTIATED_SECONDS', 600.0)
    router.set_params(
        via_cost=float(config.via_cost) * 0.1 / g, turn_cost=0.3,
        r_tt=(tw + cl) / g + m, r_tv=(tw / 2 + cl + vd / 2) / g + m,
        r_vv=max(vd + cl, dr + h2h) / g + m,
        margin=int(round(_env_num('KRT_NEGOTIATED_MARGIN_MM', 6.0) / g)),
        max_iters=_env_num('KRT_NEGOTIATED_PASSES', 60, int),
        threads=_env_num('KRT_NEGOTIATED_THREADS', 0, int),
        time_limit_s=seconds,
        pres0=_env_num('KRT_NEGOTIATED_PRES0', 0.5), pres_mult=_env_num('KRT_NEGOTIATED_PRES_MULT', 1.8),
        hist_fac=_env_num('KRT_NEGOTIATED_HIST', 0.3), h_weight=_env_num('KRT_NEGOTIATED_HWEIGHT', 1.0),
        chunk=_env_num('KRT_NEGOTIATED_CHUNK', 32, int))
    print(f"\nNegotiated main pass: {len(picked)} nets ({len(rest)} left to the sequential loop), "
          f"grid {w}x{h}x{len(layer_names)}")
    stats = router.run()
    for line in stats['log']:
        print(f"  {line}")
    print(f"  negotiation {stats['stopped']} after {stats['passes']} passes, {stats['seconds']:.1f} s, "
          f"{stats['searches']} searches, {stats['expansions']} expansions; routed {stats['routed']}, "
          f"dropped {len(stats['dropped'])}, unroutable {len(stats['unroutable'])}")

    th_cache = {}
    ok = 0
    for i, (name, nid) in enumerate(picked):
        paths = router.paths(i)
        if not paths:
            rest.append((name, nid))
            continue
        if nid not in th_cache:
            th_cache[nid] = get_same_net_through_hole_positions(pcb_data, nid, config)
        segs, vias = [], []
        for p in paths:
            s, v = path_copper(pcb_data, nid, config, p, origs[i], layer_names, coord, th_cache[nid])
            segs += s
            vias += v
        longest = max(paths, key=len)
        result = {'net_id': nid, 'new_segments': segs, 'new_vias': vias, 'iterations': 0,
                  'path_length': sum(len(p) for p in paths), 'path': longest, 'negotiated': True}
        register_success(state, nid, result)
        ok += 1
    secs = time.time() - t0
    print(f"Negotiated main pass: {ok} nets routed in {secs:.1f} s; {len(rest)} to the sequential loop")
    state.negotiated_stats = {k: stats[k] for k in ('passes', 'stopped', 'seconds', 'routed', 'dropped', 'unroutable')}
    return ok, rest, secs
