# MiniOS Session Manager

Utility suite for managing MiniOS persistent sessions from within the running system.

## Components

- **minios-session-manager** - GTK3 GUI application
- **minios-session** - CLI for session operations

## Usage

```bash
# GUI application
minios-session-manager

# Basic session management
minios-session list
minios-session create native
minios-session create luks 4GB
minios-session activate <id>
minios-session delete <id>
minios-session cleanup --days 30
minios-session status

# Export and import sessions
minios-session export <id> output.tar.zst
minios-session import archive.tar.zst
minios-session import archive.tar.zst --force-mode dynfilefs
minios-session import archive.tar.zst --auto-convert

# Copy and convert sessions
minios-session copy <id>
minios-session copy <id> --to-mode raw --size 4GB
minios-session convert <id> dynfilefs --size 4GB

# Resize sessions
minios-session resize <id> 8GB
```

## Session Modes

- **native** - Direct filesystem storage (ext2/ext3/ext4, Btrfs, XFS, etc.)
- **dynfilefs** - Expandable container files (works on any filesystem including FAT32/NTFS/exFAT)
- **raw** - Fixed-size image files (works on any filesystem including FAT32/NTFS/exFAT)
- **luks** - LUKS2-encrypted ext4 file (`changes.luks`), with no partition table or nested container

`copy` always creates a new session. `convert` replaces the source by default;
use `--new-session` to preserve it.

## Filesystem Support

- **POSIX filesystems** - Native mode and available container modes
- **FAT32/NTFS/exFAT** - DynFileFS and raw containers; LUKS containers when
  cryptsetup, loop support, and the initrd LUKS hook are available
- DynFileFS, raw, and LUKS default to 4000 MB. CLI sizes accept decimal
  `MB`/`GB`/`TB` units up to 1 TB. Raw and LUKS are capped at 4000 MB on FAT32.
- Containers only grow; shrinking is unsupported.

Creating a LUKS session prompts twice for confirmation. Other operations that
read or create LUKS data use `--password-stdin`; passphrases are never placed in
arguments or metadata.

LUKS mode and `--password-stdin` are omitted from GUI and CLI choices unless
`/run/initramfs/etc/minios-initramfs-crypt`, `cryptsetup`, and `losetup` are
available in the running system.

LUKS exports contain decrypted logical session files, not the encrypted
`changes.luks` container. Importing into LUKS creates a new encrypted container.
Only `.tar.zst` imports are accepted; archive paths and file types are validated
and extraction is bounded.

## Build

```bash
make build
sudo make install
```

## License

GPL-3.0+

## Author

crims0n <crims0n@minios.dev>
