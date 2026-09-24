# MiniOS Session Manager

## Overview

Utility suite for managing MiniOS persistent sessions from within the running system.

For the full session and boot workflow, see [Sessions and persistence](https://github.com/minios-linux/docs/blob/master/using-minios/Sessions-and-Persistence.md) and [Persistence internals](https://github.com/minios-linux/docs/blob/master/reference/boot-process/Persistence-Internals.md). The CLI reference is also available in [minios-session(1)](debian/minios-session.1).

## Components

- **minios-session-manager** - GTK3 GUI application
- **minios-session** - CLI for session operations

## Usage

Start the GUI as your regular user; privileged operations use PolicyKit. The CLI requires root, even for `list` and `status`; `--help` does not. Replace `SESSION_ID` and `SQUASHFS_ID` with numeric IDs. These examples are independent operations, not a script to run as a whole.

```bash
# GUI application
minios-session-manager

# Basic session management
sudo minios-session info --json
sudo minios-session list
sudo minios-session create native
sudo minios-session create raw 4000 --encryption luks
sudo minios-session create dynblk 16384
sudo minios-session create dynblk 16384 --compression zstd
sudo minios-session create vmdk 16384
sudo minios-session create vmdk 16384 --encryption luks
sudo minios-session create squashfs --policy shutdown
sudo minios-session create squashfs --policy manual --autosave 60
sudo minios-session activate SESSION_ID
sudo minios-session save SQUASHFS_ID
sudo minios-session settings SQUASHFS_ID --shutdown on --autosave 60
sudo minios-session delete SESSION_ID
sudo minios-session cleanup --days 30
sudo minios-session status

# Export and import non-running sessions
sudo minios-session export SESSION_ID output.tar.zst
sudo minios-session import archive.tar.zst
sudo minios-session import archive.tar.zst --force-mode vmdk
sudo minios-session import archive.tar.zst --force-encryption luks
sudo minios-session import archive.tar.zst --auto-convert

# Logical copies/conversion and physical cloning
sudo minios-session copy SESSION_ID
sudo minios-session copy SESSION_ID --to-mode vmdk --size 16384
sudo minios-session clone SESSION_ID
sudo minios-session convert SESSION_ID dynblk --size 16384 --new-session

# Grow a non-running container
sudo minios-session resize SESSION_ID 32768

# Reclaim DynFileFS, DynBlk or VMDK space without moving live data
sudo minios-session reclaim SESSION_ID --json
```

## Session Modes

| Mode | Storage | Optional LUKS2 |
| --- | --- | --- |
| `native` | Direct storage in the session directory on a compatible POSIX filesystem | No |
| `dynfilefs` | Expandable format-400 `changes.dat` and segment files, exposing an ext4 `virtual.dat` through FUSE | Yes |
| `dynblk` | Native format-1 thin block storage: `volume000.db`, `volume001.db`, and subsequent parts; supports compression | Yes, with compression disabled |
| `vmdk` | Standard split sparse `twoGbMaxExtentSparse` image: `volume.vmdk` plus `volume-s001.vmdk` and subsequent parts; no compression | Yes |
| `raw` | Fixed-size ext4 image in `changes.img` | Yes |
| `squashfs` | Exact `changes.sb` snapshot, unpacked into a RAM-backed writable layer at boot | No |

Both `dynblk` and `vmdk` use the DynBlk kernel driver and dynamically allocated `/dev/dynblkN` devices, without a FUSE process. Several volumes may coexist. They are separate session modes: renaming files or changing session metadata does not convert a container. Use `copy --to-mode` or `convert` instead.

Managed VMDK sessions contain ext4, optionally inside LUKS2, on the virtual whole disk. `import` accepts a MiniOS session archive, not an arbitrary external VMDK or a partitioned virtual-machine disk. All parts belong to one image; do not rename parts or copy a writable attached image behind the driver.

## Persistence and session operations

SquashFS can save automatically at shutdown (enabled by default) and can also save periodically every 30, 60, 120, 240, or 480 minutes. These settings are independent, and **Save Now** is available for the running SquashFS session from the tray icon or its context menu in Session Manager. Periodic saving increases CPU usage and storage writes because the current SquashFS implementation rebuilds the snapshot; one hour or longer is recommended. The 30-minute due check uses a systemd timer on systemd systems and a SysV worker on Devuan; both call the same `minios-session autosave` backend.

During the current unpack-to-RAM SquashFS mode, a newly captured and activated SquashFS snapshot can take ownership of the running session. Session Manager can then delete the old running snapshot without rebooting; other persistence modes remain protected from deletion while running.

All running-session SquashFS saves are delegated to the core MiniOS Tools `minios-squashfs-save` backend. It uses `savechanges --profile exact`, validates the captured snapshot, and atomically replaces `changes.sb` without retaining a rollback generation. The MiniOS core shutdown trigger calls the Tools backend, so automatic shutdown saving does not depend on Session Manager being installed and works with both systemd and SysV init.

Raw, DynFileFS, DynBlk, and VMDK may optionally use LUKS2 encryption. `copy` is a logical filesystem copy that creates fresh filesystem and LUKS identities and may change backend, capacity, or encryption. `clone` physically copies a detached backend and preserves its LUKS header, keyslots, LUKS UUID, and ext4 UUID. `convert` replaces the source by default; use `--new-session` to preserve it. In-place conversion requires selecting another boot-default session first.

Export, copy, clone, conversion, and resize reject the running session; `reclaim` supports running DynFileFS/DynBlk/VMDK sessions. SquashFS uses its own capture and save path; its export/import/copy/clone/conversion workflows are not provided by these generic session operations.

DynBlk creation offers only codecs that the current boot kernel/initramfs can provide through the Linux `crypto_comp` API. Session Manager inspects kmod metadata for the running kernel. When LiveKit has removed the retained module tree from `/run/initramfs`, it checks the matching boot image on the live medium and intersects its providers with the running system. Images are unpacked with `unmkinitramfs` or Dracut's `lsinitrd --unpack`; built-in providers and module dependencies are detected without loading anything. Known codecs are `none`, `lz4`, `lz4hc`, `lzo`, `lzo-rle`, `zstd`, `deflate`, and `842`; unavailable codecs are omitted. The selection applies to new DynBlk containers, including import/copy/convert targets, and is unavailable with LUKS.

## Filesystem support and sizes

Native mode needs a compatible POSIX filesystem. DynFileFS and raw containers also support writable FAT32, NTFS, and exFAT media, subject to file-size and resource limits. Both DynBlk formats require a lower filesystem admitted by their kernel driver: ext2/ext4, Btrfs, vfat, exFAT, or ntfs3 with supported geometry and attributes. This does not include every POSIX or NTFS driver; ntfs-3g/FUSE is not an accepted DynBlk backing filesystem. Use `sudo minios-session info --json` to see compatible modes on your media.

SquashFS creation and saving require a POSIX persistence filesystem for exact-capture staging: links, ownership, modes, xattrs, ACLs, capabilities, and whiteouts must be preserved. Activating an existing snapshot validates its metadata, digest and union compatibility; activation alone does not establish that the storage supports saving.

For `minios-session create`, DynFileFS and raw default to **4000 MiB**; DynBlk and VMDK default to **16384 MiB (16 GiB)** of virtual capacity. Native and SquashFS have no fixed container size.

Sizes are integer MiB counts. Bare numbers and `M`/`MB` keep that count; `G`/`GB` multiply by 1000 and `T`/`TB` by 1,000,000. Thus `16GB` means **16000 MiB**, not 16 GiB; use `16384` for exactly 16 GiB. The session CLI does not accept the driver's `GiB`/`TiB` suffixes.

Raw is capped at 4000 MiB on FAT32, with or without encryption. The session CLI caps Raw and DynFileFS requests at 1,000,000 MiB; resources and backend checks can impose lower limits. DynBlk and VMDK query their respective geometry limits:

```bash
dynblk limits --format dynblk --json
dynblk limits --format vmdk --json
```

There is no separate 512-GiB session limit for the current DynBlk formats. DynFileFS, DynBlk and VMDK expose thin virtual capacity, but initial metadata, subsequent writes and growth still require space and resources. Resize grows the container and its ext4 filesystem; shrinking is unsupported.

Both DynBlk formats keep tables on disk and a bounded metadata cache in RAM (default 1 MiB). Extent descriptions and directories grow with declared geometry, not payload fill. The cache limit excludes the filesystem page cache, codec buffers and other kernel objects. Session operations use the default `writeback` policy; other driver policies can be selected through low-level DynBlk attachment commands, not a `minios-session --cache` option.

## Availability and encryption

The GUI offers compatible modes reported by the backend; explicit CLI requests are validated before creation. DynBlk requires the `dynblk` utility, a loaded or discoverable module, and `/run/initramfs/etc/minios-initramfs-dynblk`. VMDK additionally requires `vmdk-session-v1` in that marker and support for `dynblk limits --format vmdk`. When UEFI Secure Boot is enabled, DynBlk and VMDK creation is disabled because MiniOS does not sign the external DynBlk kernel module. Update the driver, CLI, session tools and boot initrd together; updating the GUI alone does not add VMDK boot support.

LUKS2 requires cryptsetup, the selected backend's tools, and `luks-layer-v1` in `/run/initramfs/etc/minios-initramfs-crypt`. Raw and DynFileFS also use a loop device; DynBlk and VMDK pass `/dev/dynblkN` directly to cryptsetup. Their stack is `ext4 -> LUKS2/dm-crypt -> /dev/dynblkN -> backing files`. VMDK encryption means LUKS2 inside an ordinary VMDK, not VMware's native image encryption. Inner filesystem contents and metadata are encrypted; the VMDK descriptor, backend mappings, filenames and external session metadata remain outside LUKS.

Encrypted creation prompts for a passphrase and confirmation, or accepts two stdin lines with `--password-stdin` when the LUKS capability is available. Encrypted export and resize need one passphrase line with `--password-stdin`. Logical copy/convert prompts interactively unless that flag is used: supply the source passphrase if needed, then the target passphrase twice if the target is encrypted. Passphrases are not placed in arguments or session metadata. Physical cloning preserves the encrypted backend without unlocking it.

To change the LUKS passphrase of an inactive, detached encrypted Raw, DynFileFS, DynBlk, or VMDK session, use **Change Passphrase...** from its context menu or `sudo minios-session change-passphrase SESSION_ID`. The CLI prompts for the current passphrase, the new passphrase, and confirmation; `--password-stdin` reads these three lines in the same order. The new passphrase replaces the selected existing keyslot without re-encrypting files or formatting the container. An active, running, or mounted session must be detached before changing its passphrase. Additional keyslots managed outside Session Manager are not changed.

Encrypted exports contain **decrypted logical files**, not an encrypted archive. Import defaults to an unencrypted destination even for an encrypted source; `--force-encryption luks` creates a fresh encrypted backend. Only `.tar.zst` session archives are accepted; paths and types are validated and extraction is bounded.

## Mounting a detached session

Right-click an inactive, non-running Raw, DynFileFS, DynBlk, or VMDK session and choose **Mount Session** to attach its existing filesystem read-write and open it in the file manager. Encrypted sessions prompt for their LUKS passphrase. Multiple sessions can be mounted at once; operations on other session IDs remain available. The mounted row shows **MOUNTED**, and conflicting actions for that session stay disabled. **Open Session Folder** opens the numbered directory containing the backing storage; **Open Mounted Folder** opens the attached filesystem. Closing Session Manager leaves mounts intact. Reopen it to see the mounted sessions and choose **Unmount Session** when done.

The standalone CLI keeps the mount alive until standard input closes or it receives Ctrl+C. It holds a lease on that session only, never formats a failed existing mount, and refuses active or running sessions:

```bash
sudo minios-session mount SESSION_ID --json
```

If unmounting is busy, the backend reports failure and leaves the backing daemon or device attached rather than disconnecting it underneath a live filesystem. GUI-managed mounts retain their control socket and recorded mount path so Unmount can be retried after the process using the folder has left it. The standalone CLI exits after a failed unmount.

New temporary mount directories follow `/tmp/minios-session-<ID>-filesystem-*` for the folder containing session files and `/tmp/minios-session-<ID>-backend-*` for intermediate storage mounts. An in-progress conversion without a target ID uses `staging` in place of `<ID>`. Existing mounts keep their original paths until unmounted.

## Returning unused DynFileFS, DynBlk and VMDK space

Right-click a DynFileFS, DynBlk or VMDK session and choose **Free Space...**. For plaintext sessions, the backend runs FITRIM on the actual inner ext4, not the combined AUFS/OverlayFS root, then invokes the owning backend's reclaim command. A session mounted by Session Manager reuses its existing image or device, including after reopening the GUI. Detached plaintext sessions are temporarily attached and mounted. Running DynBlk/VMDK sessions keep their device; it is validated against protected current-boot state.

DynFileFS reclamation requires version 4.6.0 or later; the backend checks command support before mounting or trimming. Older versions remain usable for existing session operations. Running sessions are supported: a short-lived worker creates a recursively private mount namespace to access `virtual.dat` below MiniOS's ext4 overmount. The working session's mounts, loop device and daemon remain untouched in the original namespace. The worker verifies protected boot state, mount/loop/image identities, and the actual running daemon's reclaim ioctl before trim. Installing a new CLI alone does not upgrade an already running daemon; reboot to use the new daemon if the check fails. Temporary mounts of detached sessions are released without lazy unmount; if teardown is busy, their daemon is retained rather than killed underneath a live filesystem. DynFileFS used size is measured from allocated blocks, so hole punching is reflected in the session list.

```bash
# No live-data relocation, for either format
sudo minios-session reclaim SESSION_ID --json

# Explicitly permit in-place relocation and additional flash writes
sudo minios-session reclaim SESSION_ID --compact --json
```

Without `--compact`, ext4 backing files can return physical blocks through hole punching. On exFAT, freed interior ranges can be reused by the container, but only fully free file tails can be returned to the filesystem without moving data. **Live data is never relocated automatically**, including when hole punching is unavailable. The GUI compaction checkbox is off by default; boot and shutdown do not enable compaction.

Explicit compaction needs no conversion or second image and does not change virtual capacity. It can increase I/O latency and need not remove every gap in one pass. Encrypted sessions reclaim only space already known to the driver; this command does not unlock a detached LUKS filesystem or automatically enable discard through dm-crypt. Read-only, fenced and `unsafe` attachments are rejected; a failed filesystem trim stops the operation.

DynFileFS/DynBlk/VMDK usage is reported from allocated blocks, not just file lengths. Reclaim counters for requested punch ranges and truncated lengths are not measured physical-space savings. DynFileFS results include `allocated_before`, `allocated_after`, and `freed_bytes`; concurrent session writes can affect these measurements. Unlike low-level `dynblk reclaim`, the session command executes immediately without `--execute`; relocation still requires explicit `--compact`.

## Creating a session on another MiniOS installation

Mount the target partition and create its `minios/changes` directory first. Explicitly scope the operation with `--sessions-dir` so it does not change sessions on the running live medium:

```bash
sudo minios-session create native \
  --sessions-dir /mnt/target/minios/changes --activate --json
sudo minios-session create dynblk 16384 --compression zstd \
  --sessions-dir /mnt/target/minios/changes --activate --json
sudo minios-session create vmdk 16384 \
  --sessions-dir /mnt/target/minios/changes --activate --json
```

`--activate` publishes the completed session and its boot-default selection in one metadata update. It does not switch the running system or change `running`. Without this option, `create` leaves the boot default unchanged. Encrypted creation uses the same `--encryption luks` and `--password-stdin` interface.

Installer uses this backend rather than duplicating storage creation or metadata updates. It also checks VMDK/LUKS support and selected compression providers in the source initrds it will copy. A manual `--sessions-dir` call checks the current runtime; ensure the target installation's own initrd can resume the selected format and encryption.

## Development

Install the build and runtime dependencies listed in [debian/control](debian/control). From the repository root, compile the gettext catalogs and install the suite:

```bash
make build
sudo make install
```

## Author

crims0n <crims0n@minios.dev>

## License

GPL-3.0+
