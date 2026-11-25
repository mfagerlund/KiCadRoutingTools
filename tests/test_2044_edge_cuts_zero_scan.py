#!/usr/bin/env python3
"""`strip_zero_length_edge_cuts` gives the same text and report as before,
finding each line's closing paren with a regex jump instead of a per-character
Python loop.

The pass looks at every `(gr_line` / `(fp_line` on the board and walked up to
4000 characters one by one to find the matching `)`. On large boards that loop
was a visible slice of every output write. The walk now steps from paren to
paren; the depth rule is unchanged: every paren counts (quotes are not
special) and a group that does not close within 4000 characters is skipped.

Invariants gated here:
  1. `_close_within` equals the per-character loop on random paren/quote text,
     every start offset and limits that cut groups short.
  2. Randomised boards (zero-length and real Edge.Cuts lines, other layers,
     quoted parens, a long unclosed tail): output text and printed report equal
     the reference implementation.

Run:
    python3 tests/test_2044_edge_cuts_zero_scan.py
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

from kicad_writer import strip_zero_length_edge_cuts, _close_within

#: Pure text in, text out; no routing, no subprocesses.
RUN_ALL_FAST_OK = True


def _ref_close(content, s0, limit):
    depth = 0
    for j in range(s0, min(limit, len(content))):
        if content[j] == '(':
            depth += 1
        elif content[j] == ')':
            depth -= 1
            if depth == 0:
                return j + 1
    return None


def _reference(content):
    """The pass as it stood before the change, kept verbatim as the oracle."""
    removed = 0
    out = []
    i = 0
    for m in re.finditer(r'\((gr_line|fp_line)\b', content):
        if m.start() < i:
            continue
        s0 = m.start()
        depth = 0
        end = None
        for j in range(s0, min(s0 + 4000, len(content))):
            if content[j] == '(':
                depth += 1
            elif content[j] == ')':
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end is None:
            continue
        blk = content[s0:end]
        if '"Edge.Cuts"' in blk:
            a = re.search(r'\(start ([-\d.]+) ([-\d.]+)\)', blk)
            b = re.search(r'\(end ([-\d.]+) ([-\d.]+)\)', blk)
            if a and b and float(a.group(1)) == float(b.group(1)) \
                    and float(a.group(2)) == float(b.group(2)):
                out.append(content[i:s0])
                i = end
                removed += 1
    if not removed:
        return content
    out.append(content[i:])
    print(f"Dropped {removed} zero-length Edge.Cuts primitive(s) "
          f"(KiCad grades these as invalid_outline and slows to a crawl)")
    return ''.join(out)


def _run(fn, text):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = fn(text)
    return res, buf.getvalue()


def test_close_within_matches_loop():
    rng = random.Random(2044)
    alpha = '((()))" a\n'
    for _ in range(20000):
        s = ''.join(rng.choice(alpha) for _ in range(rng.randint(0, 40)))
        for s0 in range(len(s)):
            if s[s0] != '(':
                continue
            for limit in (s0 + 1, s0 + 5, s0 + 17, len(s), len(s) + 9):
                assert _close_within(s, s0, limit) == _ref_close(s, s0, limit), \
                    (repr(s), s0, limit)


def _line(rng, tag):
    x1, y1 = rng.choice([0, 1.5, 2]), rng.choice([0, 3, 3.0])
    if rng.random() < 0.5:
        x2, y2 = x1, y1
    else:
        x2, y2 = x1 + rng.choice([0, 1]), y1 + rng.choice([0, 0.5])
    layer = rng.choice(['"Edge.Cuts"', '"Edge.Cuts"', '"F.SilkS"'])
    extra = rng.choice(['', ' (uuid "a(b")', ' (stroke (width 0.1) (type solid))',
                        ' (note "x)y")'])
    return f'({tag} (start {x1} {y1}) (end {x2} {y2}){extra} (layer {layer}))'


def test_board_matches_reference():
    rng = random.Random(44)
    for t in range(3000):
        parts = ['(kicad_pcb (version 20240108)\n']
        for _ in range(rng.randint(0, 30)):
            k = rng.random()
            if k < 0.45:
                parts.append('  ' + _line(rng, 'gr_line') + '\n')
            elif k < 0.8:
                body = ' '.join(_line(rng, 'fp_line')
                                for _ in range(rng.randint(1, 4)))
                parts.append(f'  (footprint "R(1)" {body})\n')
            else:
                parts.append('  (gr_text "(gr_line" (at 0 0))\n')
        if t % 50 == 0:
            parts.append('  (gr_line (start 1 1) (end 1 1) (layer "Edge.Cuts") '
                         + '(x ' * 1500 + '\n')
        parts.append(')\n')
        text = ''.join(parts)
        assert _run(strip_zero_length_edge_cuts, text) == _run(_reference, text)


if __name__ == '__main__':
    test_close_within_matches_loop()
    test_board_matches_reference()
    print('ok')
