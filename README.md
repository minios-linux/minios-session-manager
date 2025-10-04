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
minios-session copy <id> --to-mode raw --size 2000
minios-session convert <id> dynfilefs --size 1000

# Resize sessions
minios-session resize <id> 2000
```

## Session Modes

- **native** - Direct filesystem storage (ext2/ext3/ext4, Btrfs, XFS, etc.)
- **dynfilefs** - Expandable container files (works on any filesystem including FAT32/NTFS/exFAT)
- **raw** - Fixed-size image files (works on any filesystem including FAT32/NTFS/exFAT)

Sessions can be converted between any modes using copy or convert commands.

## Filesystem Support

- **ext4/Btrfs/XFS** - All session modes supported
- **FAT32/NTFS/exFAT** - Only dynfilefs and raw modes supported
- Default session size: 1000MB (can be changed with --size option)

## Build

```bash
make build
sudo make install
```

## License

GPL-3.0+

## Author

crims0n <crims0n@minios.dev>