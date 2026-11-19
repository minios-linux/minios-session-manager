"""Declared DynFileFS dependency must reflect the tested MiniOS backend."""
from pathlib import Path
import re
import subprocess


def test_dynfilefs_release_based_dependency_ordering():
    control = (Path(__file__).resolve().parents[1] / 'debian/control').read_text()
    minimum = re.search(r'dynfilefs \(>= ([^)]+)\)', control).group(1)
    assert minimum == '6.04-1~minios1'
    assert subprocess.run(['dpkg', '--compare-versions',
                           '6.00~git20261112.49e5546-1~minios1', 'lt', minimum]).returncode == 0
    assert subprocess.run(['dpkg', '--compare-versions', minimum, 'lt', '6.04-1']).returncode == 0
