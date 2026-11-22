"""The final reconciliation's finer-grid rung (#1763).

A fine-pitch net whose pads sit off the run's grid can stay boxed in through
every in-run retry: the per-net rescue's finer grid (net_rescue rung 0) routes
through free space only, the terminal escalation keeps the run's grid, and the
reconciliation laps keep it too -- so the copper this same run laid around the
pads (or an earlier step's) walls them in at a grid that never changes.

This rung is ONE more reconciliation lap, after the ordinary laps, for the nets
still open whose failure was the static box-in verdict: it re-routes them at
HALF the run's grid, with rip authority over the blockers the last lap named
for them. The authority is filtered by the reconciliation's own guard (the
caller passes it in): protected nets are never granted, nets negated by plan
and plane/zone-backed nets are skipped, and locked copper is dropped by the
sub-run's own rip filter, exactly as for the #103 escalation.

The lap is whole-board (the reconciliation re-invokes batch_route on the
board), so it is bounded by a cell budget instead of a window: a board whose
half-grid map would exceed FINER_GRID_RUNG_MAX_CELLS (summed over the routing
layers) skips the rung and says so (#105: halving the grid quadruples the
cells).
"""

import routing_defaults as defaults


def rung_cells(board_bounds, grid_step: float, n_layers: int) -> float:
    """Cells of a whole-board map at `grid_step` over `n_layers` layers."""
    if not board_bounds or grid_step <= 0:
        return 0.0
    w = max(0.0, board_bounds[2] - board_bounds[0])
    h = max(0.0, board_bounds[3] - board_bounds[1])
    return (w / grid_step) * (h / grid_step) * max(1, n_layers)


def boxed_in_names(open_targets: dict, outer_boxed: set) -> list:
    """The open targets that qualify: their last lap (or the outer pass)
    recorded the static box-in verdict for them."""
    return [n for n, rec in (open_targets or {}).items()
            if (rec or {}).get('boxed_in') or n in outer_boxed]


def rung_blockers(open_targets: dict, names) -> list:
    """The blockers the last lap named for `names`, first-seen order."""
    out = []
    for n in names:
        for b in ((open_targets or {}).get(n) or {}).get('blockers') or []:
            if b not in out and b not in names:
                out.append(b)
    return out


def plan(open_targets: dict, outer_boxed: set, grid_step: float,
         board_bounds, n_layers: int):
    """Decide the rung. Returns (names, fine_grid, cells, reason): names is
    empty when the rung does not run, and `reason` then says why (None when
    there was simply nothing to do)."""
    names = boxed_in_names(open_targets, outer_boxed)
    if not names:
        return [], None, 0.0, None
    fine = grid_step / 2.0
    cells = rung_cells(board_bounds, fine, n_layers)
    if cells > defaults.FINER_GRID_RUNG_MAX_CELLS:
        return [], fine, cells, (
            f"a whole-board grid-{fine:g} map is ~{cells / 1e6:.0f}M cells "
            f"over {n_layers} layer(s), past the "
            f"{defaults.FINER_GRID_RUNG_MAX_CELLS / 1e6:.0f}M budget")
    return names, fine, cells, None


def floor_geometry(config, pcb_data, net_ids, track_width, via_size):
    """The rung lap's track width and via at the board floor (#NNNN).

    A net boxed in at the run's grid is usually boxed in at the run's WIDTH
    too: off-grid fine-pitch pads leave room for a floor-width track between
    their neighbours, not for the nominal one, and the half grid alone cannot
    make the nominal track fit. Every other retry the run makes already steps
    the width down (the per-net rescue, the terminal escalation) -- but those
    have no rip authority, and the rung, which has, kept the nominal width.

    Returns the batch_route overrides for the lap: ``track_width`` at the
    narrowest track every target may carry (the fab floor raised to each
    target's own rule minimum, ``config.track_floor``) and ``via_size`` /
    ``via_drill`` at the first escalation rung smaller than the lap's via that
    meets every target's floors. A key is omitted when it would not shrink;
    the dict is empty under --escalation off (the half grid is then the only
    change, #842)."""
    from fab_tiers import may_narrow, escalation_rungs
    from plane_pad_tap import fab_floor_clearance_track
    if not may_narrow() or not net_ids:
        return {}
    out = {}
    _c, fab_track = fab_floor_clearance_track(pcb_data)
    layers = list(config.layers or []) or [None]
    floor = max(config.track_floor(nid, layer, fab_track)
                for nid in net_ids for layer in layers)
    if track_width and floor < track_width - 1e-9:
        out['track_width'] = floor
    if via_size:
        n_layers = len(pcb_data.board_info.copper_layers) or 2
        dia = drill = 0.0
        for nid in net_ids:
            ladder = [r for r in escalation_rungs(
                n_layers, extra_floors=config.rule_floors(nid))
                if r['via_diameter'] < via_size - 1e-9]
            if not ladder:
                return out
            dia = max(dia, ladder[0]['via_diameter'])
            drill = max(drill, ladder[0]['via_drill'])
        if dia < via_size - 1e-9:
            out['via_size'], out['via_drill'] = dia, drill
    return out
