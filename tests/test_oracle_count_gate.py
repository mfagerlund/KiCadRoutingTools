"""kicad_unconnected answers [] without running kicad-cli when a refill of the SAME bytes reported KiCad's
unconnected count as 0 (2026-11-08); any other count, other bytes or no refill still run the DRC. The refill
parser records the count only when the refill ran at the board's own sibling project (or at none)."""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))

import kicad_exact_fill as kef  # noqa: E402
import kicad_oracle as ko  # noqa: E402


CALLS = []


def _boom(*a, **k):
    CALLS.append(a)
    raise OSError('kicad-cli stand-in')


def _board(tmp, text='(kicad_pcb (version 20240108))\n'):
    p = os.path.join(tmp, 'b.kicad_pcb')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(text)
    return p


def test_zero_count_skips_cli(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        b = _board(tmp)
        kef._UNCONNECTED_COUNT.clear()
        ko._UNCONNECTED_MEMO.clear()
        monkeypatch.setattr(ko.subprocess, 'run', _boom)
        kef._UNCONNECTED_COUNT[kef.unconnected_content_key(b)] = 0
        CALLS.clear()
        assert ko.kicad_unconnected(b, 'kicad-cli') == [] and not CALLS
        # other bytes: the recorded zero does not apply
        _board(tmp, '(kicad_pcb (version 20240108) (net 1 "X"))\n')
        CALLS.clear()
        assert ko.kicad_unconnected(b, 'kicad-cli') is None and CALLS, 'kicad-cli was not run'


def test_nonzero_count_runs_cli(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        b = _board(tmp)
        kef._UNCONNECTED_COUNT.clear()
        ko._UNCONNECTED_MEMO.clear()
        monkeypatch.setattr(ko.subprocess, 'run', _boom)
        kef._UNCONNECTED_COUNT[kef.unconnected_content_key(b)] = 3
        CALLS.clear()
        assert ko.kicad_unconnected(b, 'kicad-cli') is None and CALLS, 'kicad-cli was not run'


def test_project_change_is_a_different_key():
    with tempfile.TemporaryDirectory() as tmp:
        b = _board(tmp)
        k0 = kef.unconnected_content_key(b)
        with open(os.path.join(tmp, 'b.kicad_pro'), 'w', encoding='utf-8') as f:
            f.write('{}')
        assert kef.unconnected_content_key(b) != k0


def _fake_refill(count_line):
    def run(cmd, **k):
        staged, filled = cmd[2], cmd[3]
        with open(filled, 'w', encoding='utf-8') as f:
            f.write('(kicad_pcb (version 20240108))\n')
        return subprocess.CompletedProcess(cmd, 0, 'REFILL_OK 0\n' + count_line, '')
    return run


def test_refill_records_count_only_at_own_project(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        b = _board(tmp)
        monkeypatch.setattr(kef, 'find_kicad_python', lambda: 'kpy')
        monkeypatch.setattr(kef.subprocess, 'run', _fake_refill('UNCONNECTED_COUNT 0\n'))
        kef._UNCONNECTED_COUNT.clear()
        kef._REFILL_MEMO.clear()
        kef.refill_islands_ex(b)
        assert kef.known_unconnected_count(b) == 0
        # a refill at a borrowed project (project_from, no sibling) is not what kicad-cli would fill at
        os.utime(b, ns=(1, 1))
        kef._UNCONNECTED_COUNT.clear()
        kef._REFILL_MEMO.clear()
        other = os.path.join(tmp, 'o.kicad_pcb')
        with open(os.path.join(tmp, 'o.kicad_pro'), 'w', encoding='utf-8') as f:
            f.write('{}')
        kef.refill_islands_ex(b, project_from=other)
        assert kef.known_unconnected_count(b) is None
        # no count line (an older script, or the count failed): nothing recorded
        kef._REFILL_MEMO.clear()
        monkeypatch.setattr(kef.subprocess, 'run', _fake_refill(''))
        os.utime(b, ns=(2, 2))
        kef.refill_islands_ex(b)
        assert kef.known_unconnected_count(b) is None
