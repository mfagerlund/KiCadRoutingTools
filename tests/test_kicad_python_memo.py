"""find_kicad_python remembers a passed pcbnew probe across processes, keyed on the interpreter file."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'py_router'))

import kicad_exact_fill as kef  # noqa: E402


def test_probe_memo_roundtrip(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        old = tempfile.tempdir
        tempfile.tempdir = d
        try:
            exe = os.path.join(d, 'python.exe')
            with open(exe, 'w') as f:
                f.write('x')
            assert not kef._probe_remembered(exe)
            kef._remember_probe(exe)
            assert kef._probe_remembered(exe)
            os.environ['KRT_KICAD_PYTHON_PROBE'] = 'always'
            try:
                assert not kef._probe_remembered(exe)
            finally:
                del os.environ['KRT_KICAD_PYTHON_PROBE']
            with open(exe, 'w') as f:   # another build of the interpreter: a miss
                f.write('xy')
            assert not kef._probe_remembered(exe)
            assert not kef._probe_remembered(os.path.join(d, 'missing.exe'))
            # the env fast path answers from the memo without a probe or an install scan
            kef._remember_probe(exe)
            prev = os.environ.get('KICAD_PYTHON')
            os.environ['KICAD_PYTHON'] = exe
            saved = kef._KICAD_PYTHON_MEMO[:]
            kef._KICAD_PYTHON_MEMO.clear()
            real = kef.kicad_python_candidates
            kef.kicad_python_candidates = lambda: (_ for _ in ()).throw(AssertionError('scanned'))
            try:
                assert kef.find_kicad_python() == exe
            finally:
                kef.kicad_python_candidates = real
                kef._KICAD_PYTHON_MEMO[:] = saved
                os.environ.pop('KICAD_PYTHON')
                if prev is not None:
                    os.environ['KICAD_PYTHON'] = prev
        finally:
            tempfile.tempdir = old


if __name__ == '__main__':
    test_probe_memo_roundtrip()
    print('ok')
