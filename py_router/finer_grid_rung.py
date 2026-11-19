"""The final reconciliation's finer-grid rung (#1763).

A fine-pitch net whose pads sit off the run's grid can stay boxed in through
every in-run retry: the per-net rescue's finer grid (net_rescue rung 0) routes
through free space only, the terminal escalation keeps the run's grid, and the
reconciliation laps keep it too -- so the copper this same run laid around the
pads (or an earlier step's) walls them in at a grid that never changes.

This rung is ONE more reconciliation lap, after the ordinary laps, for the nets
still open whose failure was the static box-in verdict: it re-routes them at
HALF the run's grid, with rip authority over the blockers the last lap named
for them, interleaved across the targets so a capped grant reaches every
target. The authority is filtered by the reconciliation's own guard (the
caller passes it in): protected nets are never granted, nets negated by plan
and plane/zone-backed nets are skipped, and locked copper is dropped by the
sub-run's own rip filter, exactly as for the #103 escalation.

The lap is whole-board (the reconciliation re-invokes batch_route on the
board), so it is bounded by a cell budget instead of a window: a board whose
half-grid map would exceed FINER_GRID_RUNG_MAX_CELLS (summed over the routing
layers) skips the rung and says so (#105: halving the grid quadruples the
cells).

Each rung lap is GATED (lap_verdict): its rips leave a victim open when the
victim cannot re-route, and the rung has no lap after its last to retry it,
so a lap that leaves the board worse by the run's own improvement gate (more
nets broken than connected, or more pads or joins open) is undone and the
rung stops. scalenode-cm4: a rung lap connected SD_DAT1 by ripping SD_DAT2
and left SD_DAT2 and TRD2_N open.
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
    """The blockers the last lap named for `names`, ROUND-ROBIN across the
    targets: every target's first blocker, then every target's second, and
    so on, each name once.

    The caller caps the grant (route.py _RIP_ESCALATION_CAP), so the order
    decides who gets authority. First-seen order let the first targets spend
    the whole cap: on scalenode-cm4 the rung's 12 names went to
    Adapter_USB_P, TRD2_P and ETH_LEDG, and SCL0, SD_DAT1 and SD_DAT0 (the
    CM4 connector's 0.4 mm row) stayed boxed in by SD_DAT3/SD_DAT2 copper
    the rung had no right to move. Round-robin gives each target its most
    decisive blockers before any target gets its tail."""
    lists = [[b for b in (((open_targets or {}).get(n) or {}).get('blockers')
                          or []) if b not in names] for n in names]
    out = []
    for i in range(max((len(x) for x in lists), default=0)):
        for x in lists:
            if i < len(x) and x[i] not in out:
                out.append(x[i])
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


def lap_verdict(before: dict, after: dict, net_name) -> tuple:
    """(verdict, comparison) for one rung lap: the run's improvement gate
    (improvement_gate.compare_connectivity / gate_verdict) between the board
    before the lap and after it, both {net_id: (connected, pads_off[, joins])}.
    'reject' = the lap left the board worse on any axis; a draw is kept."""
    from improvement_gate import compare_connectivity, gate_verdict
    cmp = compare_connectivity(before, after, net_name)
    return gate_verdict(cmp), cmp
