"""Walled-net rip-and-reroute: the last whole-net pass for a net still open.

After the own-requirement retry, the rescue and the terminal escalation, a net
can stay open only because other nets' copper walls every corridor to one of
its pads (scalenode-cm4 SD_DAT0 / SDA0 at the J7 row: GPIO7/8/18/24/25). None
of those passes has rip authority, and the main loop's rip picks its blockers
from the cells at the edge of a failed search -- the copper nearest the pad,
not the copper across the cheapest corridor -- so it rips the wrong nets, the
victims cannot come back, and the opens move around instead of closing.

This pass picks the walling set from the corridor itself:

1. Probe. Hide every foreign net's RIPPABLE copper (a routed result, not
   locked, not refused by rip_refusal, not a diff pair, not a length-matched
   member) and route the open net whole-net on a board-global map. The probe's
   copper is then taken back; the nets whose hidden copper it collides with
   are the walling set W, ordered by how many items they put in the way.
   Copper that may not be ripped stays an obstacle during the probe, so W
   holds only nets that may be ripped.
2. Rip W, route the open net first (now on the real board, W gone), then
   reroute each victim whole-net (the board-global map sees the new copper).
   A second victim order is tried when the first strands one.
3. All or nothing: accepted only when the open net and every victim grade
   fully connected and none of the new copper stands inside a clearance of
   any foreign copper. Otherwise every new item is removed and every victim
   restored from its saved payload, so the worst case is "nothing changes".

A hard wall-time cap (KICAD_WALLED_RIP_S) bounds the pass so it never eats
the budget the final reconcile runs in; a net attempt that crosses the cap
is cancelled and rolled back. KICAD_WALLED_RIP=0 disables the pass.
"""

import fnmatch
import time
from typing import Dict, List, Optional

import env_knobs
from routing_state import record_net_event
from terminal_colors import RED, GREEN, RESET


def _lm_member(name: str, length_match_groups) -> bool:
    for grp in (length_match_groups or []):
        pats = grp if isinstance(grp, (list, tuple, set)) else [grp]
        for pat in pats:
            if isinstance(pat, str) and (pat == name
                                         or fnmatch.fnmatchcase(name, pat)):
                return True
    return False


def rippable_net(state, net_id: int, length_match_groups=None) -> bool:
    """May this pass rip `net_id` as a wall? A routed result of the run (its
    synthetic pre-existing result included), none of its copper locked or held, no
    protection refusal, not a diff pair (it rips and reroutes as a unit,
    which a single-ended reroute cannot give back), not length-matched (a
    reroute would void its match)."""
    from protected_nets import rip_refusal
    pcb_data = state.pcb_data
    if net_id not in state.routed_results or net_id not in pcb_data.nets:
        return False
    if state.diff_pair_by_net_id and net_id in state.diff_pair_by_net_id:
        return False
    if len(pcb_data.pads_by_net.get(net_id, [])) < 2:
        return False
    name = pcb_data.nets[net_id].name or ''
    if name.upper() in ('GND', 'GNDA', 'GNDD', 'AGND', 'DGND', 'PGND'):
        return False
    if _lm_member(name, length_match_groups):
        return False
    from kicad_parser import is_immutable_copper
    if any(is_immutable_copper(x) for x in pcb_data.segments
           if x.net_id == net_id):
        return False
    if any(is_immutable_copper(x) for x in pcb_data.vias
           if x.net_id == net_id):
        return False
    return rip_refusal(pcb_data, net_id, context="walled-net rip",
                       diff_pair_by_net_id=state.diff_pair_by_net_id) is None


def probe_walls(state, net_id: int, cfg, net_clearances, rippable,
                cancel_check=None) -> Optional[List[int]]:
    """The nets walling `net_id`'s cheapest corridor, most items first: route
    the net with every rippable foreign net's copper hidden, collect what that
    route collides with, take the route back. None when the net does not
    connect even then (no rip can help); [] when it connects with nothing in
    the way (the earlier passes would have taken it)."""
    from net_rescue import _attempt_net_at_geometry
    from pcb_modification import bump_copper_epoch, remove_route_from_pcb_data
    from rip_up_reroute import _saved_route_colliders

    pcb_data = state.pcb_data
    segs0, vias0 = pcb_data.segments, pcb_data.vias
    hidden_s = [s for s in segs0 if s.net_id != net_id and rippable(s.net_id)]
    hidden_v = [v for v in vias0 if v.net_id != net_id and rippable(v.net_id)]
    if not hidden_s and not hidden_v:
        return None
    _hs, _hv = {id(s) for s in hidden_s}, {id(v) for v in hidden_v}
    pcb_data.segments = [s for s in segs0 if id(s) not in _hs]
    pcb_data.vias = [v for v in vias0 if id(v) not in _hv]
    bump_copper_epoch(pcb_data)
    try:
        edges, fully = _attempt_net_at_geometry(
            pcb_data, net_id, cfg, net_clearances, cancel_check,
            scope_key=('walled_probe',))
        probe = {'new_segments': [s for r in edges for s in r['new_segments']],
                 'new_vias': [v for r in edges for v in r.get('new_vias', [])]}
        for r in reversed(edges):
            remove_route_from_pcb_data(pcb_data, r)
    finally:
        pcb_data.segments, pcb_data.vias = segs0, vias0
        bump_copper_epoch(pcb_data)
    if not fully:
        return None
    hits: Dict[int, int] = {}
    for _kind, obj in _saved_route_colliders(probe, pcb_data, [net_id],
                                             state.config.clearance,
                                             config=state.config):
        nid = getattr(obj, 'net_id', None)
        if nid is not None and nid != net_id:
            hits[nid] = hits.get(nid, 0) + 1
    return sorted(hits, key=lambda n: (-hits[n], n))


def _rip(state, nid):
    from rip_up_reroute import rip_up_net
    return rip_up_net(
        nid, state.pcb_data, state.routed_net_ids, state.routed_net_paths,
        state.routed_results, state.diff_pair_by_net_id,
        state.remaining_net_ids, state.results, state.config,
        state.track_proximity_cache, state.working_obstacles,
        state.net_obstacles_cache, state.ripped_route_layer_costs,
        state.ripped_route_via_positions, state.layer_map,
        history_conflict=False)


def _restore(state, nid, saved):
    from rip_up_reroute import restore_net
    restore_net(
        nid, saved[0], list(saved[1]), saved[2], state.pcb_data,
        state.routed_net_ids, state.routed_net_paths, state.routed_results,
        state.diff_pair_by_net_id, state.remaining_net_ids, state.results,
        state.config, state.track_proximity_cache, state.layer_map,
        state.working_obstacles, state.net_obstacles_cache,
        state.ripped_route_layer_costs, state.ripped_route_via_positions)


def _refresh_working(state, nids):
    """Keep the run's incremental map in step with committed copper."""
    if state.working_obstacles is None or state.net_obstacles_cache is None:
        return
    from obstacle_cache import (precompute_net_obstacles,
                                add_net_obstacles_from_cache,
                                remove_net_obstacles_from_cache)
    for nid in nids:
        old = state.net_obstacles_cache.get(nid)
        if old is not None:
            remove_net_obstacles_from_cache(state.working_obstacles, old)
        state.net_obstacles_cache[nid] = precompute_net_obstacles(
            state.pcb_data, nid, state.config)
        add_net_obstacles_from_cache(state.working_obstacles,
                                     state.net_obstacles_cache[nid])


def _new_copper_collides(state, edge_sets) -> bool:
    from rip_up_reroute import _saved_route_colliders
    for nid, edges in edge_sets:
        res = {'new_segments': [s for r in edges for s in r['new_segments']],
               'new_vias': [v for r in edges for v in r.get('new_vias', [])]}
        if _saved_route_colliders(res, state.pcb_data, [nid],
                                  state.config.clearance, first_only=True,
                                  config=state.config):
            return True
    return False


def _attempt_on_ladder(pcb_data, net_id, rungs, net_clearances, cancel_check,
                      scope_key):
    """Route `net_id` at each rung in turn until one connects it fully; a
    rung that does not is taken back. Returns (edges, fully)."""
    from net_rescue import _attempt_net_at_geometry
    from pcb_modification import remove_route_from_pcb_data
    edges = []
    for cfg in rungs:
        if cancel_check and cancel_check():
            break
        edges, fully = _attempt_net_at_geometry(
            pcb_data, net_id, cfg, net_clearances, cancel_check,
            scope_key=scope_key)
        if fully:
            return edges, True
        for r in reversed(edges):
            remove_route_from_pcb_data(pcb_data, r)
        edges = []
    return edges, False


def _try_net(state, net_name, net_id, walls, n_rungs, ladder_for,
             net_clearances, cancel_check):
    """Rip `walls`, route the net, reroute the victims; all or nothing.
    Returns the victim order that landed, or None (board unchanged)."""
    from net_rescue import _net_component_info
    from pcb_modification import remove_route_from_pcb_data

    pcb_data = state.pcb_data
    pe_reg = getattr(pcb_data, '_preexisting_rips', None) or {}
    pe_before = set(pe_reg)
    saved = {}
    for w in walls:
        s = _rip(state, w)
        if s[0] is None:
            continue
        saved[w] = s
    pe_new = set(getattr(pcb_data, '_preexisting_rips', None) or {}) - pe_before

    def _undo(added):
        for _nid, edges in reversed(added):
            for r in reversed(edges):
                remove_route_from_pcb_data(pcb_data, r)
        for w in reversed(list(saved)):
            _restore(state, w, saved[w])
        reg = getattr(pcb_data, '_preexisting_rips', None)
        if reg is not None:
            for k in pe_new:
                reg.pop(k, None)

    edges, fully = _attempt_on_ladder(pcb_data, net_id, n_rungs,
                                      net_clearances, cancel_check,
                                      ('walled_rip',))
    if not fully:
        _undo([(net_id, edges)])
        return None
    added_n = [(net_id, edges)]

    orders = [list(saved)]
    if len(saved) > 1:
        orders.append(list(reversed(list(saved))))
    for order in orders:
        added = []
        ok = True
        for w in order:
            if cancel_check and cancel_check():
                ok = False
                break
            ev, fv = _attempt_on_ladder(pcb_data, w, ladder_for(w),
                                        net_clearances, cancel_check,
                                        ('walled_victim',))
            added.append((w, ev))
            if not fv:
                ok = False
                break
        if ok and _net_component_info(pcb_data, net_id)[0] <= 1 \
                and not _new_copper_collides(state, added_n + added):
            _commit(state, net_name, net_id, edges, added, saved, pe_new)
            return order
        for _nid, ev in reversed(added):
            for r in reversed(ev):
                remove_route_from_pcb_data(pcb_data, r)
        if cancel_check and cancel_check():
            break
    _undo(added_n)
    return None


def _commit(state, net_name, net_id, edges, added, saved, pe_new):
    from net_rescue import _commit_whole_net
    pcb_data = state.pcb_data
    _commit_whole_net(state, net_name, net_id, edges, 'is_walled_rip')
    reg = getattr(pcb_data, '_rip_saved', None)
    for w, ev in added:
        _commit_whole_net(state, pcb_data.nets[w].name, w, ev,
                          'is_walled_rip_victim')
        state.ripped_route_layer_costs.pop(w, None) \
            if state.ripped_route_layer_costs is not None else None
        state.ripped_route_via_positions.pop(w, None) \
            if state.ripped_route_via_positions is not None else None
        # A pre-existing victim keeps its payload: the run's pre-existing
        # block reads it to strip the original copper from the output. Any
        # other victim is home; no terminal restore may bring it back twice.
        if reg is not None and w not in pe_new:
            reg.pop(w, None)
    _refresh_working(state, [net_id] + [w for w, _ in added])


def walled_net_rip(state, scope_nets, net_clearances=None,
                   progress_callback=None, cancel_check=None,
                   length_match_groups=None):
    """Run the pass over `scope_nets` [(name, id)]. Returns
    {'attempted', 'nets': {name: outcome}, 'time'} or None when no net was
    attempted (or KICAD_WALLED_RIP=0)."""
    if not env_knobs.WALLED_RIP:
        return None
    from net_rescue import _net_component_info, own_requirement_config

    pcb_data = state.pcb_data
    config = state.config
    t0 = time.time()
    deadline = t0 + max(0.0, env_knobs.WALLED_RIP_S)
    cap = max(1, env_knobs.WALLED_RIP_MAX)

    def _cancel():
        return time.time() > deadline or bool(cancel_check and cancel_check())

    candidates = []
    for net_name, net_id in sorted(set(scope_nets)):
        if net_id not in pcb_data.nets:
            continue
        if len(pcb_data.pads_by_net.get(net_id, [])) < 2:
            continue
        if state.diff_pair_by_net_id and net_id in state.diff_pair_by_net_id:
            continue
        num0 = _net_component_info(pcb_data, net_id)[0]
        if num0 > 1:
            candidates.append((net_name, net_id, num0))
    if not candidates:
        return None

    def ladder_for(nid):
        # The net's own geometry first, then the terminal escalation's rungs
        # (#1853 policy included): a net the escalation could reach only at
        # a finer rung is probed and rerouted there, never below it.
        from net_rescue import _escalation_ladder
        own = own_requirement_config(config, nid) or config
        return [own] + list(_escalation_ladder(config, pcb_data, nid) or [])

    _memo: Dict[int, bool] = {}

    def rippable(nid):
        if nid not in _memo:
            _memo[nid] = rippable_net(state, nid, length_match_groups)
        return _memo[nid]

    print(f"\nWalled-net rip-and-reroute: {len(candidates)} open net(s), "
          f"cap {env_knobs.WALLED_RIP_S:.0f}s, at most {cap} wall net(s) each")
    outcomes: Dict[str, str] = {}
    summary = {'attempted': 0, 'nets': outcomes, 'time': 0.0}
    for idx, (net_name, net_id, num0) in enumerate(candidates):
        if _cancel():
            print("  walled rip: time cap reached")
            break
        if progress_callback:
            progress_callback(idx + 1, len(candidates),
                              f"Walled-net rip: {net_name}")
        t_net = time.time()
        walls, n_rungs = None, []
        rungs = ladder_for(net_id)
        for k, cfg in enumerate(rungs):
            if _cancel():
                break
            walls = probe_walls(state, net_id, cfg, net_clearances,
                                rippable, _cancel)
            if walls is not None:
                n_rungs = rungs[k:]
                break
        if walls is None:
            outcomes[net_name] = 'no corridor even without rippable copper'
            print(f"  walled rip: {net_name}: no corridor even with every "
                  f"rippable net hidden")
            continue
        if not walls:
            outcomes[net_name] = 'corridor free, nothing to rip'
            print(f"  walled rip: {net_name}: corridor free (nothing to rip)")
            continue
        names = [pcb_data.nets[w].name for w in walls]
        if len(walls) > cap:
            outcomes[net_name] = f"walled by {len(walls)} nets (> {cap})"
            print(f"  walled rip: {net_name}: walled by {len(walls)} nets "
                  f"(cap {cap}): {', '.join(names)}")
            continue
        summary['attempted'] += 1
        print(f"  walled rip: {net_name}: ripping {', '.join(names)}")
        order = _try_net(state, net_name, net_id, walls, n_rungs, ladder_for,
                         net_clearances, _cancel)
        dt = time.time() - t_net
        if order is None:
            outcomes[net_name] = 'rolled back'
            record_net_event(state, net_id, "walled_rip_failed",
                             {"walls": names, "time_s": round(dt, 2)})
            print(f"    {RED}walled rip: {net_name} rolled back{RESET} "
                  f"({dt:.1f}s)")
            continue
        outcomes[net_name] = ('recovered, rerouted '
                              + ', '.join(pcb_data.nets[w].name for w in order))
        record_net_event(state, net_id, "walled_rip_succeeded",
                         {"walls": names, "components_before": num0,
                          "time_s": round(dt, 2)})
        print(f"    {GREEN}walled rip: {net_name} recovered{RESET} "
              f"({num0} -> 1 parts; {len(order)} victim(s) rerouted, "
              f"{dt:.1f}s)")
        _memo.clear()
    summary['time'] = round(time.time() - t0, 2)
    if not summary['attempted']:
        return None
    rec = sum(1 for v in outcomes.values() if v.startswith('recovered'))
    print(f"Walled-net rip: {rec} recovered, {summary['attempted'] - rec} "
          f"rolled back ({summary['time']:.1f}s)")
    return summary
