#!/usr/bin/env python3
"""qfn_fanout under-pad via-in-pad on a 0.5 mm pitch row whose pads sit OFF the
routing grid keeps each via on its pad's centre line across the escape axis.

The under-pad candidate loop snapped BOTH via coordinates to the grid. Along the
escape axis that is harmless (the ladder steps there anyway); across it, it moves
the via sideways toward a neighbour pad. On a 0.5 mm pitch row placed 0.044 mm off
a 0.1 mm grid (C10 Revenant U901, pads at x = ...856) a 0.45 mm via in a 0.25 mm
wide pad keeps 0.030 mm of slack to the neighbour at 0.12 mm clearance when
centred, and loses it when shifted 0.044 mm: every in-pad candidate failed and the
pad was dropped ("no clear via offset").

The board is the checked-in HVQFN-32 repro (kicad_files/qfn_diffpair_escape),
rewritten in a temp dir: the footprint moved to x = 11.106 so the bottom row lands
at ...356/...856, its routed copper stripped, and four adjacent bottom-row pads
given their own nets. Needs no pcbnew.
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # #522
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # #522
from kicad_parser import parse_kicad_pcb
from kicad_writer import add_tracks_and_vias_to_pcb
from qfn_fanout import generate_qfn_fanout
from check_drc import run_drc

BASE = os.path.join(ROOT, "kicad_files", "qfn_diffpair_escape.kicad_pcb")
VIA_SIZE, VIA_DRILL, CLEARANCE, GRID = 0.45, 0.2, 0.12, 0.1
ROW = ("10", "11", "12", "13")


def _offgrid_board(path):
    with open(BASE, encoding="utf-8") as f:
        text = f.read()
    text = text.replace("(at 11 11)", "(at 11.106 11)", 1)
    text = re.sub(r"\n\t\(segment\n.*?\n\t\)", "", text, flags=re.S)
    for num in ROW:
        text = re.sub(r'(\(pad "%s" smd roundrect\n)' % num,
                      r'\1\t\t\t(net "ROW%s")\n' % num, text, count=1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        board = os.path.join(tmp, "offgrid.kicad_pcb")
        _offgrid_board(board)
        pcb = parse_kicad_pcb(board)
        u1 = pcb.footprints["U1"]
        pads = {p.pad_number: p for p in u1.pads if p.pad_number in ROW}
        if sorted(pads) != sorted(ROW) or any(p.net_name != "ROW" + n
                                              for n, p in pads.items()):
            print(f"FAIL: fixture rewrite did not net the row: "
                  f"{[(n, p.net_name) for n, p in pads.items()]}")
            return 1
        off = [n for n, p in pads.items()
               if abs(p.global_x / GRID - round(p.global_x / GRID)) < 0.1]
        if off:
            fails.append(f"fixture pads {off} are on the grid; the case needs them off")

        tracks, vias, dropped = generate_qfn_fanout(
            u1, pcb, net_filter=["ROW*"], layer="F.Cu",
            track_width=0.1, clearance=CLEARANCE, grid_step=GRID,
            escape_method="underpad", via_size=VIA_SIZE, via_drill=VIA_DRILL,
            allow_via_in_pad=True)
        if dropped:
            fails.append(f"dropped (every row pad should escape in pad): {dropped}")
        by_net = {p.net_id: p for p in pads.values()}
        for v in vias:
            p = by_net.get(v["net_id"])
            if p is None:
                continue
            if abs(v["x"] - p.global_x) > 1e-6:
                fails.append(f"pad {p.pad_number} via x {v['x']:.4f} left the pad "
                             f"centre line {p.global_x:.4f} (cross-axis snapped)")
        if len(vias) != len(ROW):
            fails.append(f"expected {len(ROW)} vias, got {len(vias)}")

        out = os.path.join(tmp, "out.kicad_pcb")
        add_tracks_and_vias_to_pcb(board, out, tracks, vias,
                                   net_id_to_name={nid: n.name for nid, n in pcb.nets.items()})
        bad = [v for v in run_drc(out, clearance=CLEARANCE, quiet=True,
                                  check_sizes=False) if not v.get('accepted')]
        if bad:
            fails.append(f"fanout shipped {len(bad)} DRC violation(s): {bad[:2]}")

    if fails:
        print("FAIL: " + "; ".join(fails))
        return 1
    print("PASS: off-grid 0.5 mm pitch row escapes in pad, each via on its pad's "
          "cross-axis centre, DRC-clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
