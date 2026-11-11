"""parse_kicad_pcb's bytes memo: a repeat parse equals a fresh parse, every caller gets its own copy, and a changed
file is parsed again."""
import os
import pickle
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

import kicad_parser as K  # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'sonde_u.kicad_pcb')


def _blob(pcb):
    return pickle.dumps(pcb, protocol=pickle.HIGHEST_PROTOCOL)


def test_memo_hit_equals_fresh_parse_and_is_a_copy():
    K._PARSE_BYTES_MEMO.clear()
    first = K.parse_kicad_pcb(BOARD)
    second = K.parse_kicad_pcb(BOARD)
    assert second is not first
    # against the stored bytes: an unpickle round trip can change object sharing, not the values
    stored = next(iter(K._PARSE_BYTES_MEMO.values()))
    assert _blob(K.parse_kicad_pcb_uncached(BOARD)) == stored
    first.segments.append('mutated')
    assert 'mutated' not in K.parse_kicad_pcb(BOARD).segments


def test_changed_file_is_parsed_again():
    K._PARSE_BYTES_MEMO.clear()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'b.kicad_pcb')
        shutil.copy(BOARD, p)
        n0 = len(K.parse_kicad_pcb(p).segments)
        with open(p, 'rb') as f:
            raw = f.read()
        tail = raw.rstrip()
        assert tail.endswith(b')')
        seg = b'\n  (segment (start 1 1) (end 2 1) (width 0.2) (layer "F.Cu") (net 0) (uuid "memo-test"))\n'
        with open(p, 'wb') as f:
            f.write(tail[:-1] + seg + b')\n')
        assert len(K.parse_kicad_pcb(p).segments) == n0 + 1
        assert len(K._PARSE_BYTES_MEMO) == 2


if __name__ == '__main__':
    test_memo_hit_equals_fresh_parse_and_is_a_copy()
    test_changed_file_is_parsed_again()
    print('PASS')
