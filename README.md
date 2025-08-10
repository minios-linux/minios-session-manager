# MiniOS Session Manager

Utility suite for managing MiniOS persistent sessions from within the running system.

## Components

- **minios-session-manager** - GTK3 GUI application
- **minios-session** - CLI for session operations

## Usage

```bash
# GUI application
minios-session-manager

# CLI commands
minios-session list
minios-session create --mode native
minios-session activate <id>
minios-session cleanup --days 30
```

## Session Modes

- **native** - Direct filesystem storage (POSIX filesystems)
- **dynfilefs** - Expandable container files (FAT32/NTFS)
- **raw** - Fixed-size image files (any filesystem)

## Build

```bash
make build
sudo make install
```

## License

GPL-3.0+

## Author

crims0n <crims0n@minios.dev>