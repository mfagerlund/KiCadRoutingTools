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
KRT_NEGOTIATED_MARGIN_MM (search window margin around a net's terminals, 6 mm), KRT_NEGOTIATED_CHUNK (nets searched
together, 16), KRT_NEGOTIATED_MAX_TERMINALS (nets with more terminals go to the sequential loop, 32),
KRT_NEGOTIATED_HWEIGHT (A* heuristic weight; config.heuristic_weight), KRT_NEGOTIATED_PRES0 / _PRES_MULT / _HIST
/ _PRES_MAX
(PathFinder present-cost start, growth and cap, history increment), KRT_NEGOTIATED_ASYNC=1 (a work queue instead
of chunked rounds: each search commits once checked against the commits made since it began; not deterministic),
KRT_NEGOTIATED_MIN_NETS (40: smaller sets, and every call after the first in a process, go straight to the
sequential loop), KRT_NEGOTIATED_NO_FALLBACK (diagnosis: nothing goes to the sequential loop).
"""
import math
import os
import types
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


_RAN = [False]


def wanted(n_nets: int) -> bool:
    """Whether this batch_route call gets the negotiated pass: KRT_NEGOTIATED set, the first call in this process,
    and at least KRT_NEGOTIATED_MIN_NETS (40) nets. Rescue rounds re-enter batch_route with a handful of hard nets;
    there the negotiation only repeats the sequential loop's work (cm5io: 7 calls, 144 s, 7 nets)."""
    if not enabled() or _RAN[0] or n_nets < _env_num('KRT_NEGOTIATED_MIN_NETS', 40, int):
        return False
    _RAN[0] = True
    return True


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
    out, rows = [], []  # rows: one pad_info row per terminal (the pin's shape, the pad object or a stand-in sixth)
    if mp:
        for e in mp:
            obj = e[5] if len(e) > 5 else None
            if obj is not None and hasattr(obj, 'global_x') and hasattr(obj, 'size_x'):
                cells, orig = pad_terminal_cells(obj, coord, layer_map, config.layers, tw)
                out.append((cells, {c: orig for c in cells[:len(cells)]}))
            else:
                c = (e[0], e[1], e[2])
                out.append(([c], {c: (e[3], e[4])}))
            rows.append(tuple(e))
    else:
        sources, targets, err = get_net_endpoints(pcb_data, net_id, config)
        if not sources or not targets:
            return None
        for side in (sources, targets):
            cells = [(s[0], s[1], s[2]) for s in side]
            out.append((cells, {(s[0], s[1], s[2]): (s[3], s[4]) for s in side}))
            rows.append(_terminal_row(pcb_data, net_id, config, side[0]))
    keep = [i for i, t in enumerate(out) if t[0]]
    out = _join_by_existing_copper(pcb_data, net_id, config, coord, [out[i] for i in keep], [rows[i] for i in keep])
    return out if len(out) >= 2 else None


def _terminal_row(pcb_data, net_id, config, s):
    """A pad_info row for an endpoint row (gx, gy, layer, x, y): the pad at that point when there is one, else a
    stand-in with the layer name, which the copper join matches to a stub's free end as the pin does."""
    for pad in pcb_data.pads_by_net.get(net_id, []):
        if abs(pad.global_x - s[3]) < 1e-3 and abs(pad.global_y - s[4]) < 1e-3:
            return (s[0], s[1], s[2], s[3], s[4], pad)
    layer = config.layers[s[2]] if s[2] < len(config.layers) else None
    return (s[0], s[1], s[2], s[3], s[4], types.SimpleNamespace(layer=layer))


def _join_by_existing_copper(pcb_data, net_id, config, coord, terms, rows):
    """The net's existing copper (fanout dogbones and vias, escape routes, stubs) joins the terminals it connects
    into one terminal and is part of it, cell by cell on every layer it spans, as the pin's main pass models it
    ("Existing copper joins N terminals into M groups"; the island cells of its launch surface). A pad under a
    BGA whose fanout via already leads to an inner layer is then reached through that copper, not through the pad
    field: with bare pads as terminals ten Revenant nets, every one with a fanout, were unroutable in the negotiation
    (2026-11-07), the search exhausted inside the SiP's exclusion zone or at a pad no via may sit on."""
    if os.environ.get('KRT_NEG_NO_COPPER_JOIN'):  # diagnosis knob
        return terms
    if not any(s.net_id == net_id for s in pcb_data.segments) and not any(v.net_id == net_id for v in pcb_data.vias):
        return terms
    from connectivity import get_terminal_component_info
    from single_ended_routing import get_all_segment_tap_points
    comps, _, segs_by, vias_by = get_terminal_component_info(pcb_data, net_id, list(rows))
    groups = {}
    for i, t in enumerate(terms):
        groups.setdefault(comps.get(i, ('lone', i)), []).append(t)
    out = []
    for cid, members in groups.items():
        cells, orig = [], {}
        for mc, mo in members:
            for c in mc:
                if c not in orig:
                    cells.append(c)
                    orig[c] = mo.get(c, coord.to_float(c[0], c[1]))
        if not isinstance(cid, tuple):
            for p in get_all_segment_tap_points(segs_by.get(cid, []), coord, config.layers, vias=vias_by.get(cid, [])):
                c = (p[0], p[1], p[2])
                if c not in orig:
                    cells.append(c)
                    orig[c] = (p[3], p[4])
        out.append((cells, orig))
    if os.environ.get('KRT_NEGOTIATED_DEBUG_SEALED'):
        print(f'    copper-join net {net_id}: {[len(c) for c, _ in terms]} terminals -> {[len(c) for c, _ in out]} (segs_by keys {list(segs_by)}, vias_by keys {list(vias_by)}; comps {comps}; segs {sum(len(v) for v in segs_by.values())}, vias {sum(len(v) for v in vias_by.values())})')
    return out


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



class _ElapsedTee:
    """stdout unchanged, plus a copy of every line stamped with wall-clock seconds since the tee began and, after it,
    the process's own CPU seconds (`<wall> <cpu>c <line>`, 2026-11-07): the CPU column does not move with another
    process's load on the machine, so a stage can be judged on it when the wall clock cannot be trusted."""

    def __init__(self, inner, path):
        self.inner, self.f, self.t0, self.bol = inner, open(path, 'a', encoding='utf-8'), time.time(), True
        self.c0 = time.process_time()

    def write(self, s):
        self.inner.write(s)
        for part in s.splitlines(True):
            if self.bol:
                self.f.write(f'{time.time() - self.t0:8.1f} {time.process_time() - self.c0:7.1f}c ')
            self.f.write(part)
            self.bol = part.endswith('\n')
        self.f.flush()
        return len(s)

    def flush(self):
        self.inner.flush()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _install_elapsed_tee():
    """By default (KRT_NEGOTIATED_ELAPSED_LOG=0 turns it off): copy stdout, stamped, to krt-elapsed.log beside the output board (where the time
    goes after the negotiation; the route log itself is unchanged)."""
    import sys
    if os.environ.get('KRT_NEGOTIATED_ELAPSED_LOG', '1') in ('', '0') or isinstance(sys.stdout, _ElapsedTee):
        return
    outs = [a for a in sys.argv if a.endswith('.kicad_pcb')]
    if outs:
        sys.stdout = _ElapsedTee(sys.stdout, os.path.join(os.path.dirname(os.path.abspath(outs[-1])), 'krt-elapsed.log'))


def _debug_sealed(state, nid, picked, origs, index, coord, layer_names):
    """KRT_NEGOTIATED_DEBUG_SEALED=1: for every terminal cell of an unroutable net, what blocks it: the base map
    (permanent copper), and which nets' keep-out caches cover the cell (the net's own cache is passable)."""
    base = state.base_obstacles
    cache = state.net_obstacles_cache or {}
    orig = origs[index[nid]]
    owners = {}
    for onid, cd in cache.items():
        cells, _ = _cache_cells_and_vias(cd)
        for row in cells:
            owners.setdefault((int(row[0]), int(row[1]), int(row[2])), set()).add(int(onid))
    for cell, (ox, oy) in sorted(orig.items()):
        x, y, l = cell
        blocked = base.is_blocked_hard(int(x), int(y), int(l))
        vb = base.is_via_blocked_hard(int(x), int(y))
        zone = base.in_bga_zone(int(x), int(y))
        own = owners.get((x, y, l), set())
        print(f"      cell ({x},{y},{layer_names[l] if l < len(layer_names) else l}) at ({ox:.2f},{oy:.2f}): "
              f"base {'BLOCKED' if blocked else 'free'}, via {'blocked' if vb else 'free'}{', in BGA zone' if zone else ''}, "
              f"keep-outs {sorted(own - {int(nid)})}{' +own' if int(nid) in own else ''}")
        # the copper of the net itself at that point, from the board
        pcb = state.pcb_data
        near = [f"seg {s.layer} ({s.start_x:.2f},{s.start_y:.2f})-({s.end_x:.2f},{s.end_y:.2f}){' locked' if getattr(s, 'locked', False) else ''}"
                for s in pcb.segments if s.net_id == nid and min(abs(s.start_x - ox) + abs(s.start_y - oy), abs(s.end_x - ox) + abs(s.end_y - oy)) < 0.05]
        near += [f"via ({v.x:.2f},{v.y:.2f}){' locked' if getattr(v, 'locked', False) else ''}"
                 for v in pcb.vias if v.net_id == nid and abs(v.x - ox) + abs(v.y - oy) < 0.05]
        if near:
            print("        own copper here: " + '; '.join(near[:4]))
        if blocked:
            # foreign copper within reach of the cell: what the base map stamped there
            nm = lambda n: pcb.net_id_to_name.get(n) or (pcb.nets[n].name if n in pcb.nets and hasattr(pcb.nets[n], 'name') else str(n))
            reach = 0.5
            foreign = [f"pad {pd.component_ref}.{pd.pad_number} [{nm(pd.net_id)}] {pd.size_x:.2f}x{pd.size_y:.2f} at ({pd.global_x:.2f},{pd.global_y:.2f}) d={math.hypot(pd.global_x - ox, pd.global_y - oy):.2f}"
                       for pads in pcb.pads_by_net.values() for pd in pads
                       if pd.net_id != nid and math.hypot(pd.global_x - ox, pd.global_y - oy) < reach]
            foreign += [f"via [{nm(v.net_id)}] {v.size:.2f} at ({v.x:.2f},{v.y:.2f}) d={math.hypot(v.x - ox, v.y - oy):.2f}{' locked' if getattr(v, 'locked', False) else ''}"
                        for v in pcb.vias if v.net_id != nid and math.hypot(v.x - ox, v.y - oy) < reach]
            foreign += [f"seg [{nm(sg.net_id)}] {sg.layer} w{sg.width:.2f} ({sg.start_x:.2f},{sg.start_y:.2f})-({sg.end_x:.2f},{sg.end_y:.2f}){' locked' if getattr(sg, 'locked', False) else ''}"
                        for sg in pcb.segments if sg.net_id != nid and min(math.hypot(sg.start_x - ox, sg.start_y - oy), math.hypot(sg.end_x - ox, sg.end_y - oy)) < reach]
            print("        foreign copper within 0.5 mm: " + ('; '.join(foreign[:8]) if foreign else 'none'))
            shown = getattr(state, '_sealed_maps_shown', 0)
            if shown < 6:
                state._sealed_maps_shown = shown + 1
                r = 8
                print(f"        base map {2 * r + 1}x{2 * r + 1} cells around it on {layer_names[l] if l < len(layer_names) else l} "
                      f"(# blocked, z zone only, . free, X the cell; v = via blocked):")
                for dy in range(-r, r + 1):
                    row = ''
                    for dx in range(-r, r + 1):
                        b = base.is_blocked_hard(int(x + dx), int(y + dy), int(l))
                        z = base.in_bga_zone(int(x + dx), int(y + dy))
                        row += 'X' if (dx == 0 and dy == 0) else ('#' if b else ('z' if z else ('v' if base.is_via_blocked_hard(int(x + dx), int(y + dy)) else '.')))
                    print('          ' + row)


def route_negotiated(state, single_ended_nets: List[Tuple[str, int]], cancel_check=None):
    """Route single_ended_nets by negotiated congestion. Returns (successful, rest, seconds): rest are the
    (name, id) the sequential loop still has to route."""
    t0 = time.time()
    _install_elapsed_tee()
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

    # KRT's layer costs: a negative cost forbids tracks on that layer (route.py appends the copper layers the
    # caller did not list, the plane layers, with cost -1); through vias still span it
    forbidden = [c < 0 for c in config.get_layer_costs()]
    router.set_layer_forbidden(forbidden)

    picked, rest, origs, terms_of = [], [], [], []
    max_terms = _env_num('KRT_NEGOTIATED_MAX_TERMINALS', 32, int)
    # Global-plan corridors (trial, 2026-11-08): KRT_CORRIDORS names a JSON file {net name: [[x0, y0, x1, y1,
    # [z...]], ...]} in board mm, z = copper index top to bottom (tscircuit's capacity-mesh nodes each net's
    # connections cross); KRT_CORRIDOR_FACTOR prices a step outside them (default 3).
    corridors = None
    n_corr = 0
    corridor_factor = _env_num('KRT_CORRIDOR_FACTOR', 3.0)
    if os.environ.get('KRT_CORRIDORS'):
        import json as _json
        with open(os.environ['KRT_CORRIDORS'], encoding='utf-8') as _f:
            corridors = _json.load(_f)
    # board copper order (F.Cu, In1.Cu, ..., B.Cu) to this router's layer index
    _copper = list(pcb_data.board_info.copper_layers)
    layer_map_z = {z: layer_names.index(n) for z, n in enumerate(_copper) if n in layer_names}
    tw = config.track_width
    t_terms = 0.0
    for name, nid in single_ended_nets:
        if nid in olinks:
            rest.append((name, nid))
            continue
        t0_terms = time.perf_counter()
        terms = net_terminals(pcb_data, nid, config, coord, layer_map)
        t_terms += time.perf_counter() - t0_terms
        if not terms or len(terms) > max_terms:
            rest.append((name, nid))  # a plane-like tree (hundreds of pads) is cheaper net by net, after the rest
            continue
        # a wide (power / class) net keeps (its width - the routing width) / 2 more from everything
        net_w = max(config.get_net_track_width(nid, ln) for ln in layer_names)
        extra = max(0.0, (net_w - tw) / 2.0 / g)
        if config.bga_exclusion_zones:
            # KRT's window: a BGA exclusion zone blocks every cell on every layer except +-allow_radius cells
            # around the net's own terminals (single_ended_routing allow_radius = 10); without it a fanout via
            # under a SiP/BGA is sealed and the net falls to the sequential loop
            router.add_allowed(int(nid), [(x - 10, y - 10, x + 10, y + 10) for t in terms for (x, y, _) in t[0]])
        own_vias = sorted({tuple(int(v) for v in coord.to_grid(v.x, v.y)) for v in pcb_data.vias if v.net_id == nid})
        if os.environ.get('KRT_NEG_NO_OWN_VIAS'):  # diagnosis knob
            own_vias = []
        ni = router.add_net(int(nid), int(nid), [t[0] for t in terms], extra=extra, own_vias=own_vias)
        if corridors is not None:
            rects = corridors.get(name)
            if rects:
                cells = []
                for x0_, y0_, x1_, y1_, zs in rects:
                    a = coord.to_grid(x0_, y0_)
                    b = coord.to_grid(x1_, y1_)
                    mask = 0
                    for z in zs:
                        if z in layer_map_z:
                            mask |= 1 << layer_map_z[z]
                    cells.append((int(min(a[0], b[0])), int(min(a[1], b[1])),
                                  int(max(a[0], b[0])), int(max(a[1], b[1])), mask))
                router.set_corridor(ni, cells, corridor_factor)
                n_corr += 1
        orig = {}
        for _, o in terms:
            orig.update(o)
        picked.append((name, nid))
        origs.append(orig)
        terms_of.append([t[0] for t in terms])

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
        hist_fac=_env_num('KRT_NEGOTIATED_HIST', 0.3), h_weight=_env_num('KRT_NEGOTIATED_HWEIGHT', float(config.heuristic_weight)),
        chunk=_env_num('KRT_NEGOTIATED_CHUNK', 16, int), stop_file=stop or '',
        pres_max=_env_num('KRT_NEGOTIATED_PRES_MAX', 30.0),
        **({'async_mode': True} if os.environ.get('KRT_NEGOTIATED_ASYNC', '') not in ('', '0') else {}))
    if corridors is not None:
        print(f"Negotiated main pass: corridors for {n_corr} of {len(picked)} nets (factor {corridor_factor:g})")
    print(f"\nNegotiated main pass: {len(picked)} nets ({len(rest)} left to the sequential loop), "
          f"grid {w}x{h}x{len(layer_names)}; terminals built in {t_terms:.1f} s")
    stats = router.run()
    for line in stats['log']:
        print(f"  {line}")
    print(f"  negotiation {stats['stopped']} after {stats['passes']} passes, {stats['seconds']:.1f} s, "
          f"{stats['searches']} searches, {stats['expansions']} expansions; routed {stats['routed']}, "
          f"dropped {len(stats['dropped'])}, unroutable {len(stats['unroutable'])}")
    names = {nid: name for name, nid in picked}
    index = {nid: i for i, (_, nid) in enumerate(picked)}
    for key in ('unroutable', 'dropped'):
        if stats[key]:
            print(f"  negotiation {key}: {', '.join(names.get(n, str(n)) for n in stats[key])}")
    for n in stats['unroutable']:
        # per terminal: (cells, cells a track may stand on, cells with a free neighbour or via, cells on a
        # forbidden layer): a terminal with no free cell is sealed in by hard copper
        rep = router.terminal_report(index[n])
        sealed = sum(1 for t in rep if t[2] == 0)
        print(f"    {names.get(n, n)}: {len(rep)} terminals, {sealed} sealed; " + ' '.join(f"{t[0]}/{t[1]}/{t[2]}/{t[3]}" for t in rep))
        # the search itself, on the whole grid: how many terminals it joined, whether the expansion cap stopped it,
        # and how close it got to each terminal it never reached (a seal at the terminal reads as a few cells;
        # a wall across the board reads as many)
        if not os.environ.get('KRT_NEGOTIATED_DEBUG_SEALED'):
            continue  # the whole-grid diagnosis search costs a full search per net: debugging only
        joined, cap, exp, near = router.fail_report(index[n])
        why = 'expansion cap' if cap else 'search exhausted'
        reach = '; '.join(f"terminal {ti}: nearest reach {d:.1f} cells at ({x},{y},{layer_names[l] if l < len(layer_names) else l})"
                          for ti, d, x, y, l in near)
        print(f"      joined {joined} of {len(rep)} terminals, {why} after {exp} expansions on the whole grid" + (f"; {reach}" if reach else ''))
        if os.environ.get('KRT_NEGOTIATED_DEBUG_SEALED'):
            # what the search sees at the unreached terminal's first cell and at the nearest reached cell, per layer
            terms = terms_of[index[n]]
            for ti, d, x, y, l in near:
                spots = [('nearest reach', x, y)]
                if terms and ti < len(terms) and terms[ti]:
                    tx, ty, _ = terms[ti][0]
                    spots.insert(0, ('terminal', tx, ty))
                for what, cx, cy in spots:
                    rows = router.cell_report(index[n], cx, cy)
                    desc = ' '.join(f"{layer_names[li] if li < len(layer_names) else li}:"
                                    f"{'T' if r[0] else 't'}{'' if r[1] < 0 else f'(o{r[1]})'}{'H' if r[2] else ''}"
                                    f"{'V' if r[3] else 'v'}{'' if r[4] < 0 else f'(o{r[4]})'}{'X' if r[5] else ''}"
                                    f"{'Z' if r[6] else ''}{'A' if r[7] else ''}" for li, r in enumerate(rows))
                    print(f"        {what} ({cx},{cy}): {desc}   [T/t track ok/not, (oN) keep-out owner, H hard copper, "
                          f"V/v via ok/not, X hard via block, Z BGA zone, A allowed window]")
        if os.environ.get('KRT_NEGOTIATED_DEBUG_SEALED'):
            _debug_sealed(state, n, picked, origs, index, coord, layer_names)

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
