#!/usr/bin/env python3
"""Verify the staged/extracted backend, not imports from the source checkout."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = Path(sys.argv[1]).resolve()
    library = root / 'usr/lib/minios-session-manager'
    backend = library / 'minios_session.py'
    for path in (backend,):
        if not path.is_file() or path.is_symlink():
            raise RuntimeError('Missing installed backend file: {}'.format(path))
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    environment['LC_ALL'] = 'C'
    with tempfile.TemporaryDirectory(prefix='minios-installed-backend-') as scratch:
        result = subprocess.run(
            [sys.executable, '-I', '-B', str(backend), '--internal-dynfilefs-reclaim'],
            cwd=scratch, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10)
    if (result.returncode != 1 or result.stdout or
            result.stderr.decode('utf-8').strip() != 'Invalid internal DynFileFS reclaim arguments'):
        raise RuntimeError('Installed backend internal reclaim mode did not start correctly: {}'.format(
            result.stderr.decode('utf-8', errors='replace')))
    print('PASS installed backend: internal DynFileFS reclaim mode starts without a separate worker file')


if __name__ == '__main__':
    main()
