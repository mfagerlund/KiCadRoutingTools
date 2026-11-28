#!/usr/bin/env python3
"""A pad with a negative solder_mask_margin gets an opening band.

KiCad's DRC bridges an exposed pad whose mask opening is shrunk inside its
copper (negative solder_mask_margin) to foreign copper within 0.175 mm of
the pad COPPER, whatever the margin (mask_artwork.EXPOSED_PAD_REACH). The
mask resolver turns such a pad into an opening band owned by its net:

  1. the band is the pad copper grown by EXPOSED_PAD_REACH less the
     keep-out's MASK_REGISTRATION, on the pad's copper side;
  2. derive_opening_bands binds every other net to it, not the pad's own;
  3. a pad with a zero or positive margin, and an unnetted pad, get none.

    python3 tests/test_exposed_pad_mask_band.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

import kicad_parser  # noqa: E402
import mask_artwork  # noqa: E402

FAILS = []


def check(name, cond, detail=''):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ''))
    if not cond:
        FAILS.append(name)


BOARD = '''(kicad_pcb
	(version 20241229)
	(generator "pcbnew")
	(general (thickness 1.6))
	(layers
		(0 "F.Cu" signal)
		(2 "B.Cu" signal)
		(5 "F.SilkS" user)
		(1 "F.Mask" user)
		(3 "B.Mask" user)
		(25 "Edge.Cuts" user)
	)
	(net 0 "")
	(net 1 "GND")
	(net 2 "SIG")
	(footprint "test:EP"
		(layer "B.Cu")
		(at 10 20 180)
		(property "Reference" "U1" (at 0 0 0) (layer "B.SilkS"))
		(pad "1" smd rect (at -3 0 180) (size 0.3 1) (layers "B.Cu" "B.Mask")
			(net 2 "SIG"))
		(pad "9" smd rect (at 0 0 180) (size 4 4) (layers "B.Cu" "B.Mask" "B.Paste")
			(net 1 "GND") (solder_mask_margin -1.2))
	)
	(footprint "test:R"
		(layer "F.Cu")
		(at 30 20 0)
		(property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
		(pad "1" smd rect (at -1 0 0) (size 1 1) (layers "F.Cu" "F.Mask")
			(net 1 "GND") (solder_mask_margin 0.05))
		(pad "2" smd rect (at 1 0 0) (size 1 1) (layers "F.Cu" "F.Mask")
			(solder_mask_margin -0.3))
	)
)
'''


def main():
    d = tempfile.mkdtemp(prefix='krt-ep-band-', dir=os.path.join(ROOT, 'tests'))
    path = os.path.join(d, 'ep.kicad_pcb')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(BOARD)
        pcb = kicad_parser.parse_kicad_pcb(path)
        bands = mask_artwork._exposed_pad_bands(BOARD, pcb.footprints)
        check("one band (the netted negative-margin pad)", len(bands) == 1, str(len(bands)))
        if bands:
            b = bands[0]
            xs = [p[0] for p in b['polygon']]
            ys = [p[1] for p in b['polygon']]
            g = mask_artwork.EXPOSED_PAD_REACH - mask_artwork.MASK_REGISTRATION
            want = (8 - g, 12 + g, 18 - g, 22 + g)
            got = (min(xs), max(xs), min(ys), max(ys))
            check("band = copper + reach - registration",
                  all(abs(a - w) < 1e-6 for a, w in zip(got, want)), f"{got} vs {want}")
            check("band on B.Cu / B.Mask", b['layer'] == 'B.Cu' and b['mask'] == 'B.Mask')
            check("band owned by GND", pcb.nets[b['net_id']].name == 'GND')
        pcb.board_info.mask_keepouts = True
        regs = mask_artwork.derive_opening_bands(pcb)
        check("derive emits the band", len(regs) == 1, str(len(regs)))
        if regs:
            r = regs[0]
            check("binds SIG, not GND", 'SIG' in r['nets'] and 'GND' not in r['nets'],
                  str(sorted(r['nets'])))
            reach = (r['clearance'] + mask_artwork.EXPOSED_PAD_REACH
                     - mask_artwork.MASK_REGISTRATION)
            check("foreign copper held EXPOSED_PAD_REACH off the copper",
                  abs(reach - mask_artwork.EXPOSED_PAD_REACH) < 1e-9, f"{reach}")
    finally:
        for n in os.listdir(d):
            os.remove(os.path.join(d, n))
        os.rmdir(d)
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
