"""Boot codec detection after LiveKit has removed its initramfs module tree."""
import os
import subprocess
from unittest.mock import patch

import pytest

import minios_session as session


def test_missing_retained_modules_checks_matching_boot_image(tmp_path):
    boot = tmp_path / 'boot'
    boot.mkdir()
    image = boot / 'initrfs-test-kernel.img'
    image.write_bytes(b'fixture')
    (boot / 'initrfs.img').symlink_to(image.name)

    def unpack(path, destination):
        assert path == str(image)
        os.makedirs(os.path.join(destination, 'usr/lib/modules/test-kernel'))
        return True

    with patch.object(session, 'DYNBLK_BOOT_DIRECTORIES', (str(boot),)), \
         patch.object(session, '_unpack_dynblk_initrd', side_effect=unpack), \
         patch.object(session, '_dynblk_codecs_from_tree', side_effect=[
             ('none', 'lz4', 'zstd'), ('none', 'zstd', '842')]):
        assert session.runtime_dynblk_compression_codecs(kernel='test-kernel') == ('none', 'zstd')


@pytest.mark.parametrize('unpack_ok', [False, True])
def test_unreadable_or_other_kernel_initrd_never_claims_codecs(tmp_path, unpack_ok):
    image = tmp_path / 'initrfs.img'
    image.write_bytes(b'fixture')

    def unpack(path, destination):
        if unpack_ok:
            os.makedirs(os.path.join(destination, 'lib/modules/other-kernel'))
        return unpack_ok

    with patch.object(session, 'DYNBLK_BOOT_DIRECTORIES', (str(tmp_path),)), \
         patch.object(session, '_unpack_dynblk_initrd', side_effect=unpack), \
         patch.object(session, '_dynblk_codecs_from_tree', return_value=('none', 'lz4')):
        assert session.runtime_dynblk_compression_codecs(kernel='test-kernel') == ('none',)


def test_missing_boot_image_does_not_use_running_modules_alone(tmp_path):
    with patch.object(session, 'DYNBLK_BOOT_DIRECTORIES', (str(tmp_path),)), \
         patch.object(session, '_dynblk_codecs_from_tree', return_value=('none', 'zstd')):
        assert session.runtime_dynblk_compression_codecs(kernel='test-kernel') == ('none',)


def test_retained_tree_with_no_codecs_is_authoritative(tmp_path):
    (tmp_path / 'lib/modules/test-kernel').mkdir(parents=True)
    with patch.object(session, '_dynblk_codecs_from_tree', return_value=('none',)), \
         patch.object(session, '_unpack_dynblk_initrd') as unpack:
        assert session.runtime_dynblk_compression_codecs(str(tmp_path), 'test-kernel') == ('none',)
    unpack.assert_not_called()


@pytest.mark.parametrize('tool', ['unmkinitramfs', 'lsinitrd'])
def test_boot_unpack_resolves_symlink_and_never_uses_current_directory(tmp_path, tool):
    image = tmp_path / 'initrfs-test.img'
    image.write_bytes(b'fixture')
    alias = tmp_path / 'initrfs.img'
    alias.symlink_to(image.name)
    destination = tmp_path / 'out'
    destination.mkdir()
    executable = '/usr/bin/' + tool
    with patch.object(session.shutil, 'which', side_effect=lambda name:
                      executable if name == tool else None), \
         patch.object(session.subprocess, 'run', return_value=
                      subprocess.CompletedProcess([], 0)) as run:
        assert session._unpack_dynblk_initrd(str(alias), str(destination))
    assert str(image) in run.call_args[0][0]
    assert str(alias) not in run.call_args[0][0]
    assert run.call_args[1]['cwd'] == str(destination)


def test_boot_unpack_requires_one_supported_tool(tmp_path):
    with patch.object(session.shutil, 'which', return_value=None), \
         patch.object(session.subprocess, 'run') as run:
        assert not session._unpack_dynblk_initrd(str(tmp_path / 'image'), str(tmp_path))
    run.assert_not_called()
