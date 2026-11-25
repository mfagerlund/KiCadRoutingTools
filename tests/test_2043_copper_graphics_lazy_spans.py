#!/usr/bin/env python3
"""`move_copper_graphics_to_silkscreen` gives the same text and the same
report as before, while walking the footprint blocks far less often.

The pass loops over 12 graphic tags, and each loop re-took
`_functional_footprint_spans` -- a balanced walk over every footprint block on
the board -- even when no shape of that tag was a candidate and even when the
previous loop had relocated nothing (so no offset had moved). On large boards
that walk ran 12 times per output write. The spans are now taken only when a
net-less copper `fp_*` shape asks for them, and taken again only after a loop
has actually rewritten the text. The per-block paren walk is the same bare
depth count (quoted parens included), done with a regex jump.

Invariants gated here:
  1. Randomised boards (logos, pad-bearing footprints with copper shapes,
     net-tied graphics, quoted parens, enough relocations to drift offsets):
     output text and printed report equal the reference implementation.
  2. A board with no net-less copper `fp_*` shape never walks the footprints.
  3. A board whose loops relocate nothing walks the footprints at most once.
  4. Spans are retaken after a loop that relocated: 80 moved board logos
     drift a pad-bearing footprint past its own length.

Run:
    python3 tests/test_2043_copper_graphics_lazy_spans.py
"""

import contextlib
import io
import os
import random
import re
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_router'))
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_tools'))

import kicad_writer
from kicad_writer import (move_copper_graphics_to_silkscreen,
                          _COPPER_GRAPHIC_TAGS, _FP_MODELLED_TAGS,
                          _functional_footprint_spans, _in_any_span)

#: Pure text in, text out; no routing, no subprocesses.
RUN_ALL_FAST_OK = True


def _reference(content):
    """The pass as it stood before the change, kept verbatim as the oracle."""
    kept = 0
    count = 0
    for tag in _COPPER_GRAPHIC_TAGS:
        _keep_spans = _functional_footprint_spans(content)
        token = '(' + tag
        tlen = len(token)
        result_parts = []
        last_end = 0
        i = 0
        while True:
            start = content.find(token, i)
            if start == -1:
                break
            nxt = content[start + tlen: start + tlen + 1]
            if nxt and (nxt.isalnum() or nxt == '_'):
                i = start + tlen
                continue
            depth = 0
            end = start
            for j in range(start, len(content)):
                if content[j] == '(':
                    depth += 1
                elif content[j] == ')':
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        break
            block = content[start:end]
            layer_match = re.search(r'\(layer\s+"(F\.Cu|B\.Cu)"\)', block)
            net_match = re.search(r'\(net\s+(\d+)(?:\s+"([^"]*)")?\)', block)
            net_tied = net_match and (int(net_match.group(1)) != 0
                                      or (net_match.group(2) or '') != '')
            if not net_tied:
                name_match = re.search(r'\(net\s+"((?:[^"\\]|\\.)*)"\)', block)
                net_tied = bool(name_match and name_match.group(1) != '')
            if (layer_match and not net_tied and tag in _FP_MODELLED_TAGS
                    and _in_any_span(start, _keep_spans)):
                kept += 1
                layer_match = None
            if layer_match and not net_tied:
                layer = layer_match.group(1)
                new_layer = 'F.SilkS' if layer == 'F.Cu' else 'B.SilkS'
                block = block.replace(f'(layer "{layer}")', f'(layer "{new_layer}")')
                count += 1
            result_parts.append(content[last_end:start])
            result_parts.append(block)
            last_end = end
            i = end
        result_parts.append(content[last_end:])
        content = ''.join(result_parts)
    if count > 0:
        print(f"  Moved {count} copper graphic(s)/logo(s) to silkscreen")
    if kept > 0:
        print(f"  Kept {kept} footprint copper shape(s) on copper "
              f"(a part's own land pattern, not decoration)")
    return content


def _run(fn, text):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(text)
    return out, buf.getvalue()


_LAYERS = ('F.Cu', 'B.Cu', 'F.SilkS', 'B.SilkS', 'F.Fab')


def _shape(rng, prefix):
    tag = rng.choice(('poly', 'line', 'circle', 'arc', 'rect', 'curve'))
    layer = rng.choice(_LAYERS)
    net = ''
    if prefix == 'gr' and rng.random() < 0.3:
        net = rng.choice((' (net 1 "/N1")', ' (net 0 "")', ' (net "GND")',
                          ' (net "")', ' (net 2)'))
    if tag == 'poly':
        geo = '(pts (xy 0 0) (xy 1 0) (xy 1 1))'
    elif tag in ('circle',):
        geo = '(center 0 0) (end 1 0)'
    elif tag == 'arc':
        geo = '(start 0 0) (mid 1 1) (end 2 0)'
    elif tag == 'curve':
        geo = '(pts (xy 0 0) (xy 1 1) (xy 2 1) (xy 3 0))'
    else:
        geo = '(start 0 0) (end 1 1)'
    return ('(%s_%s %s (stroke (width 0.1) (type solid)) (layer "%s")%s '
            '(uuid "u"))' % (prefix, tag, geo, layer, net))


def _footprint(rng, idx):
    pads = rng.choice((0, 0, 1, 2, 3))
    ref = rng.choice(('U%d' % idx, 'LOGO%d' % idx, 'U1'))
    mpn = rng.choice(('X', 'TCR2EF115,LM(CT', 'A)B', 'q\\"(x'))
    body = ['(footprint "L:P%d" (layer "F.Cu") (at %d 0)' % (idx, idx),
            ' (property "Reference" "%s")' % ref,
            ' (property "MPN" "%s")' % mpn]
    for p in range(pads):
        body.append(' (pad "%d" smd rect (at %d 0) (size 0.6 0.6) '
                    '(layers "F.Cu") (net 1 "/N1"))' % (p + 1, p))
    for _ in range(rng.randint(0, 4)):
        body.append(' ' + _shape(rng, 'fp'))
    return '\n'.join(body) + ')'


def _random_board(rng):
    parts = []
    for k in range(rng.randint(0, 40)):
        parts.append(_footprint(rng, k) if rng.random() < 0.6
                     else _shape(rng, 'gr'))
    return ('(kicad_pcb (version 20240108)\n (net 0 "")\n (net 1 "/N1")\n'
            '%s\n)' % '\n'.join(parts))


def _count_walks(text):
    calls = [0]
    real = kicad_writer._functional_footprint_spans

    def counting(content):
        calls[0] += 1
        return real(content)
    kicad_writer._functional_footprint_spans = counting
    try:
        _run(move_copper_graphics_to_silkscreen, text)
    finally:
        kicad_writer._functional_footprint_spans = real
    return calls[0]


def test_matches_reference_on_random_boards():
    rng = random.Random(2043)
    moved_any = kept_any = 0
    for trial in range(400):
        text = _random_board(rng)
        want = _run(_reference, text)
        got = _run(move_copper_graphics_to_silkscreen, text)
        assert got == want, 'trial %d differs' % trial
        moved_any += 'Moved' in want[1]
        kept_any += 'Kept' in want[1]
    # The corpus must exercise both arms, or equality proves little.
    assert moved_any > 50 and kept_any > 50, (moved_any, kept_any)


def test_no_candidate_never_walks():
    text = ('(kicad_pcb (version 20240108)\n'
            '(footprint "L:P" (layer "F.Cu") (at 0 0)\n'
            ' (property "Reference" "U1")\n'
            ' (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
            ' (fp_line (start 0 0) (end 1 1) (layer "F.SilkS")))\n'
            '(gr_line (start 0 0) (end 1 1) (layer "F.Cu") (net 1 "/N1"))\n)')
    assert _count_walks(text) == 0


def test_nothing_moved_walks_once():
    fp = ('(footprint "L:P" (layer "F.Cu") (at 0 0)\n'
          ' (property "Reference" "U%d")\n'
          ' (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
          ' (fp_poly (pts (xy 0 0) (xy 1 0) (xy 1 1)) (layer "F.Cu"))\n'
          ' (fp_line (start 0 0) (end 1 1) (layer "B.Cu"))\n'
          ' (fp_arc (start 0 0) (mid 1 1) (end 2 0) (layer "F.Cu")))')
    text = '(kicad_pcb\n%s\n)' % '\n'.join(fp % k for k in range(5))
    assert _count_walks(text) == 1
    out, report = _run(move_copper_graphics_to_silkscreen, text)
    assert out == text and 'Kept 15' in report and 'Moved' not in report


def test_drift_after_relocation():
    """Each relocation grows the text by 3 bytes. 80 net-less board logos
    moved in the gr_poly loop shift a short pad-bearing footprint by 240
    bytes, past its own length, before the fp_line loop asks; spans kept
    from before that rewrite would relocate the part's own copper."""
    logos = '\n'.join('(gr_poly (pts (xy 0 0) (xy 1 0) (xy 1 1)) '
                      '(layer "F.Cu"))' for _ in range(80))
    fp = ('(footprint "L:P" (at 0 0) (property "Reference" "U1")\n'
          ' (pad "1" smd rect (layers "F.Cu"))\n'
          ' (fp_poly (pts (xy 0 0)) (layer "B.Cu"))\n'
          ' (fp_line (start 0 0) (end 1 1) (layer "F.Cu")))')
    tail = '(footprint "L:Q" (at 9 9) (property "Reference" "LOGO1") %s)' % (
        ' '.join('(fp_line (start 0 0) (end 1 1) (layer "B.SilkS"))'
                 for _ in range(40)))
    text = '(kicad_pcb\n%s\n%s\n%s\n)' % (logos, fp, tail)
    want = _run(_reference, text)
    assert 'Kept 2' in want[1] and 'Moved 80' in want[1], want[1]
    assert _run(move_copper_graphics_to_silkscreen, text) == want


if __name__ == '__main__':
    test_matches_reference_on_random_boards()
    test_no_candidate_never_walks()
    test_nothing_moved_walks_once()
    test_drift_after_relocation()
    print('test_2043_copper_graphics_lazy_spans: PASS')
