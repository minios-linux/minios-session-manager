# MiniOS Session Manager

Utility suite for managing MiniOS persistent sessions from within the running system.

Numbered SquashFS sessions, persistence alert integration, save policies, and
cross-component contracts are defined in the
[project checkpoint](https://github.com/minios-linux/minios-live/blob/master/projects/minios-session-manager.md).

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
printf '%s\n%s\n' "$PASSPHRASE" "$PASSPHRASE" | \
  minios-session create raw 4GB --encryption luks --password-stdin
minios-session create dynblk 16GB
minios-session create dynblk 16GB --compression zstd
minios-session create squashfs --policy shutdown
minios-session create squashfs --policy manual --autosave 60
minios-session activate <id>
minios-session save <running-squashfs-id>
minios-session settings <squashfs-id> --shutdown on --autosave 60
minios-session delete <id>
minios-session cleanup --days 30
minios-session status

# Export and import sessions
minios-session export <id> output.tar.zst
minios-session import archive.tar.zst
minios-session import archive.tar.zst --force-mode dynfilefs
minios-session import archive.tar.zst --force-encryption none
minios-session import archive.tar.zst --auto-convert

# Copy and convert sessions
minios-session copy <id>
minios-session copy <id> --to-mode raw --size 4GB
minios-session clone <id>
minios-session convert <id> dynfilefs --to-encryption none --size 4GB

# Resize sessions
minios-session resize <id> 8GB
```

## Session Modes

- **native** - Direct filesystem storage (ext2/ext3/ext4, Btrfs, XFS, etc.)
- **dynfilefs** - Expandable container files (works on any filesystem including FAT32/NTFS/exFAT)
- **DynBlk** (`dynblk`) - Format-1 kernel block device with thin backing files; exposed only when the running kernel and initramfs support it
- **raw** - Fixed-size image files (works on any filesystem including FAT32/NTFS/exFAT)
- **squashfs** - Exact compressed snapshots stored as a single `changes.sb`;
  creation captures the current live changes and activation selects the snapshot
  for the next boot

SquashFS can save automatically at shutdown (enabled by default) and can also
save periodically every 30, 60, 120, 240, or 480 minutes. These settings are
independent, and **Save Now** remains available at any time from the tray icon or
the SquashFS session context menu in Session Manager. Periodic saving increases CPU usage and storage writes because
the current SquashFS implementation rebuilds the snapshot; one hour or longer is
recommended. The 30-minute due check uses a systemd timer on systemd systems and a
SysV worker on Devuan; both call the same `minios-session autosave` backend.

During the current unpack-to-RAM SquashFS mode, a newly captured and activated
SquashFS snapshot can take ownership of the running session. Session Manager can
then delete the old running snapshot without rebooting; other persistence modes
remain protected from deletion while running.

All running-session SquashFS saves are delegated to the core MiniOS Tools
`minios-squashfs-save` backend. It uses `savechanges --profile exact`, validates
the captured snapshot, and atomically replaces `changes.sb` without retaining a
rollback generation. The MiniOS core shutdown trigger calls the Tools backend, so
automatic shutdown saving does not depend on Session Manager being installed and
works with both systemd and SysV init.

Raw, DynFileFS, and DynBlk may optionally use LUKS2 encryption. `copy` is a
logical filesystem copy that creates fresh filesystem and LUKS identities and
may change backend, capacity, or encryption. `clone` physically copies a
detached backend and preserves its LUKS header, keyslots, LUKS UUID, and ext4
UUID. `convert` replaces the source by default; use `--new-session` to preserve
it.

DynBlk creation can use `none`, `lz4`, `lz4hc`, `lzo`, `lzo-rle`, `zstd`,
`deflate`, or `842` compression. Compression is selected when a new DynBlk
container is created (including import/copy/convert targets) and is not available
for LUKS-encrypted DynBlk storage.

## Filesystem Support

- **POSIX filesystems** - Native mode and available container modes
- **FAT32/NTFS/exFAT** - DynFileFS and raw containers; DynBlk where the lower filesystem is admitted by its kernel driver; optional LUKS encryption when
  cryptsetup and the required backend-specific initrd hooks are available
- SquashFS save currently requires a POSIX persistence filesystem because its
  private exact-capture staging must preserve links, ownership, modes, xattrs,
  ACLs, capabilities, and whiteouts. FAT32/NTFS/exFAT activation remains gated
  until a metadata-capable bounded workspace is implemented.
- DynFileFS and raw default to 4000 MB. DynBlk defaults to a 16 GiB virtual device and is capped at 512 GiB. CLI sizes accept decimal
  `MB`/`GB`/`TB` units; raw is capped at 4000 MB on FAT32 whether encrypted or not.
- DynFileFS and DynBlk expose thin virtual capacity: their configured logical size does not require that much free space up front. Both can grow later; shrinking is unsupported.

Creating an encrypted session prompts twice for confirmation. Other operations
that read or create encrypted data use `--password-stdin`; passphrases are never
placed in arguments or metadata. Logical encrypted-to-encrypted copy and convert
read the source passphrase first, followed by the target passphrase twice.

LUKS encryption and `--password-stdin` are omitted from GUI and CLI choices
unless `/run/initramfs/etc/minios-initramfs-crypt` contains `luks-layer-v1` and
the tools needed by the selected backend are available. DynBlk is likewise omitted unless
`/run/initramfs/etc/minios-initramfs-dynblk`, the `dynblk` utility, and the
matching kernel module are available.

Encrypted exports contain decrypted logical session files, not the encrypted
backend. Importing with `--force-encryption luks` creates a fresh encrypted
backend.
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
