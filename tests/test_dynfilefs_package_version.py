"""DynFileFS userspace is optional; only Trixie packages it by default."""
from pathlib import Path
import re
import subprocess


def test_dynfilefs_optional_release_based_dependency():
    control = (Path(__file__).resolve().parents[1] / 'debian/control').read_text()
    runtime = control.split('Package: minios-session\n', 1)[1].split('\nPackage:', 1)[0]
    depends = next(line for line in runtime.splitlines() if line.startswith('Depends:'))
    assert 'dynfilefs' not in depends
    minimum = re.search(r'Suggests: dynfilefs \(>= ([^)]+)\)', runtime).group(1)
    assert minimum == '6.04-1~minios1'
    assert subprocess.run(['dpkg', '--compare-versions',
                           '6.00~git20261112.49e5546-1~minios1', 'lt', minimum]).returncode == 0
    assert subprocess.run(['dpkg', '--compare-versions', minimum, 'lt', '6.04-1']).returncode == 0
