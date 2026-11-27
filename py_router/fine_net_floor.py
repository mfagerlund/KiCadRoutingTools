"""KICAD_FINE_NET_FLOOR: signal nets with a fine-pitch pin route at the
board's minimum track width; every other net keeps its own.

A signal net routed at the run's width (say 0.1778 mm at 0.1548 clearance)
needs a 0.51 mm gap to leave a pin; a 0.4 mm-pitch QFN row has 0.2 mm, so
those pins are left to the escape reservation and the late per-net rescue,
which narrows only into whatever room the finished board has left. The
board itself allows far narrower copper (its min_track_width). With the knob
on, the run's reserve width (config.track_width, what every obstacle stamp
reserves for the future track) drops to that floor, the fine-pin nets route
at it, and every other net is pinned to the width it had through
net_track_widths, whose extra half-width rides its own A* track margin
(routing_config._phase_exact_margin) -- so a wide net is held to the same
clearance as before.

Only the single-ended engine: impedance layer widths (layer_widths) and the
diff engine's baked widths are left alone (the knob does nothing there).
"""


def floor_width(config, net_id, layer, pcb_data):
    """The narrowest width `net_id` may ship on `layer`: the wider of the fab
    tier's nominal track floor (raised to the board's own floors under
    --escalation board) and the net's .kicad_dru / Board Setup minimum."""
    from fab_tiers import fab_floors
    n = len(getattr(pcb_data.board_info, 'copper_layers', None) or []) or 2
    fab = float(fab_floors(n).get('track_width') or 0.0)
    rule = float((config.rule_floors(net_id, layer) or {}).get('track_width') or 0.0)
    return max(fab, rule)


def apply(pcb_data, config, net_ids, verbose=False):
    """Pin every net of `net_ids` to its width, then drop the reserve to the
    floor of the fine-pin nets. Returns {net_id: floor width} of the nets
    narrowed (empty: nothing to gain, config untouched)."""
    import escape_reserve as er
    import fine_escape as fe
    if getattr(config, 'layer_widths', None) or getattr(config, 'reserve_layer_widths', False):
        print("Fine-net floor: inert (impedance layer widths in this run)")
        return {}
    power = set(getattr(config, 'power_net_widths', None) or {})
    fines = er._fine_pins(pcb_data, config, [n for n in net_ids if n not in power],
                          fe._footprints(pcb_data))
    fine_nets = sorted({p.net_id for _, _, fine, _ in fines for p in fine})
    base = config.track_width
    narrow, was = {}, {}
    for nid in fine_nets:
        pads = pcb_data.pads_by_net.get(nid, [])
        layers = {l for p in pads for l in p.layers if l in config.layers} or set(config.layers)
        w = max(floor_width(config, nid, l, pcb_data) for l in layers)
        cur = min(config.get_net_track_width(nid, l) for l in config.layers)
        if 0.0 < w < cur - 1e-9:
            narrow[nid], was[nid] = w, cur
    if not narrow:
        print("Fine-net floor: armed (KICAD_FINE_NET_FLOOR), no fine-pin net "
              "narrower than its width")
        return {}
    widths = dict(config.net_track_widths or {})
    for nid in net_ids:
        if nid not in narrow and nid not in widths and nid not in power:
            widths[nid] = config.get_net_track_width(nid, config.layers[0])
    widths.update(narrow)
    config.net_track_widths = widths
    config.track_width = min(narrow.values())
    config.fine_floor_from = was
    print(f"Fine-net floor: armed (KICAD_FINE_NET_FLOOR): {len(narrow)} fine-pin "
          f"net(s) route at {config.track_width:.4f}mm (reserve {base:.4f} -> "
          f"{config.track_width:.4f}mm); {len(widths) - len(narrow)} other net(s) "
          f"keep their width")
    if verbose:
        names = {p.net_id: p.net_name for ps in pcb_data.pads_by_net.values() for p in ps}
        for nid, w in sorted(narrow.items()):
            print(f"  fine-net floor: {names.get(nid, nid)} at {w:.4f}mm")
    return narrow


def apply_relax(pcb_data, config, net_ids, verbose=False):
    """KICAD_RELAX_WIDTH: every signal net keeps its width but may neck to
    the rule floor where it does not fit. The reserve drops to the narrowest
    floor and every net is pinned at its width (net_track_widths), so the
    neck ladder (_route_main_connection) sees each one as wider than the
    layer width and narrows it to the floor (_neck_width_for_net) on a pad
    entry that is too tight. Run after apply() when both knobs are on.
    Returns {net_id: floor width} of the nets that gained a neck."""
    if getattr(config, 'layer_widths', None) or getattr(config, 'reserve_layer_widths', False):
        print("Relax width: inert (impedance layer widths in this run)")
        return {}
    power = set(getattr(config, 'power_net_widths', None) or {})
    base = config.track_width
    widths = dict(config.net_track_widths or {})
    floors, was = {}, dict(getattr(config, 'fine_floor_from', None) or {})
    for nid in net_ids:
        if nid in power:
            continue
        cur = min(config.get_net_track_width(nid, l) for l in config.layers)
        w = max(floor_width(config, nid, l, pcb_data) for l in config.layers)
        widths.setdefault(nid, config.get_net_track_width(nid, config.layers[0]))
        if 0.0 < w < cur - 1e-9:
            floors[nid] = w
    if not floors:
        print("Relax width: armed (KICAD_RELAX_WIDTH), no net wider than its floor")
        return {}
    config.net_track_widths = widths
    config.track_width = min(base, min(floors.values()))
    for nid in floors:
        was.setdefault(nid, widths[nid])
    config.fine_floor_from = was
    print(f"Relax width: armed (KICAD_RELAX_WIDTH): {len(floors)} net(s) may neck "
          f"to the floor (reserve {base:.4f} -> {config.track_width:.4f}mm)")
    return floors
