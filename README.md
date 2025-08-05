# MiniOS Session Manager

A utility suite for managing MiniOS persistent sessions from within the running system.

## Components

This package provides two complementary tools:

- **minios-session-cli**: Command-line interface for session operations
- **minios-session-manager**: GTK3 graphical interface that uses the CLI utility

## Features

- **List Sessions**: View all available sessions with detailed information (size, version, last modified)
- **Current Session**: Display information about the currently active session
- **Create Sessions**: Create new sessions with choice of persistence mode (native, dynfilefs, raw)
- **Activate Sessions**: Switch the default session for next boot
- **Delete Sessions**: Remove old or unused sessions (cannot delete active session)
- **Cleanup**: Automatically remove sessions older than specified days
- **All Session Types**: Full support for native, dynfilefs, and raw persistence modes
- **Dual Interface**: Both command-line and graphical user interfaces
- **Format Support**: Compatible with both JSON and legacy session configuration formats
- **Safety**: Prevents deletion of currently active sessions
- **Privilege Management**: Automatic privilege escalation when needed using PolicyKit/pkexec
- **User-Friendly**: Runs without root privileges, requests authentication only when necessary

## Installation

### From Source

```bash
git clone https://github.com/minios-linux/minios-live.git
cd minios-live/submodules/minios-session-manager
sudo make install
```

### Build Debian Package

```bash
make build-deb
sudo dpkg -i ../minios-session-manager_*.deb
```

## Usage

### Command Line Interface

```bash
# List all sessions
minios-session-cli list

# Show current session
minios-session-cli current

# Create new session with native mode (default)
minios-session-cli create

# Create new session with dynfilefs mode
minios-session-cli create --mode dynfilefs

# Create new session with raw mode
minios-session-cli create --mode raw

# Activate session #2
minios-session-cli activate 2

# Delete a specific session
minios-session-cli delete 3

# Clean up sessions older than 30 days
minios-session-cli cleanup --days 30
```

### Graphical Interface

Launch the GUI from:
- Application menu: System → MiniOS Session Manager
- Command line: `minios-session-manager`
- Direct execution: `python3 lib/session_manager.py`

## Dependencies

- **Required**: Python 3, python3-gi, GTK3 (for GUI), dynfilefs (for dynfilefs sessions), policykit-1 (for privilege escalation)
- **Recommended**: jq (for better JSON handling)

## Session Storage

Sessions are stored in one of these locations:
- `/run/initramfs/changes/` (primary)
- `/mnt/live/changes/` (alternative)
- `/live/changes/` (fallback)

Session metadata is stored in:
- `session.json` (modern format, preferred)
- `session.conf` (legacy format, fallback)

## Session Management

### Session Structure

Each session is stored in a numbered directory (e.g., `1`, `2`, `3`) and contains:
- Persistent changes specific to that session
- Metadata including MiniOS version, edition, and persistence mode

### Session Modes

- **native**: Direct storage on POSIX-compatible filesystems (ext4, btrfs, xfs, etc.)
- **dynfilefs**: Dynamic file-based storage using DynFileFS FUSE utility - ideal for FAT32/NTFS filesystems with automatic file splitting at 4GB boundaries
- **raw**: Fixed-size image file storage compatible with any filesystem

### Filesystem Compatibility Matrix

The tool automatically detects your media's filesystem and shows only compatible session modes:

| Filesystem | Native | DynFileFS | Raw | Notes |
|------------|--------|-----------|-----|-------|
| ext2/3/4   | ✓      | ✓         | ✓   | Full POSIX support, all modes recommended |
| btrfs      | ✓      | ✓         | ✓   | Modern filesystem, all modes work |
| xfs        | ✓      | ✓         | ✓   | High-performance filesystem |
| FAT32      | ✗      | ✓         | ✓¹  | No POSIX support, 4GB file size limit |
| NTFS       | ✗      | ✓         | ✓   | No POSIX support |
| exFAT      | ✗      | ✓         | ✓   | No POSIX support |

¹ Raw mode on FAT32 is limited to maximum 4GB image size

### Size Limitations

- **FAT32**: Maximum 4GB per file (affects raw mode images)
- **Native mode**: Limited only by available disk space
- **DynFileFS mode**: Automatically splits files at 4GB boundaries to work around filesystem limits
- **Raw mode**: Pre-allocated size, cannot be easily expanded later

### Compatibility Checking

The tool automatically checks version and edition compatibility:
- **Version**: MiniOS version (e.g., 3.3.1)
- **Edition**: Desktop environment (e.g., XFCE, Flux)

Incompatible sessions are highlighted and require confirmation before use.

## Examples

### List Sessions with Details
```
$ minios-session-cli list
Available Sessions:
--------------------------------------------------------------------------------
Session #1 (CURRENT)
  Mode: native
  Version: 3.3.1 / XFCE
  Size: 1.2GB
  Last Modified: 2025-08-05 14:30:22

Session #2 
  Mode: dynfilefs
  Version: 3.3.0 / XFCE
  Size: 856.3MB
  Last Modified: 2025-07-28 09:15:41
```

### Current Session Info
```
$ minios-session-cli current
Current session: #1
Mode: native
Version: 3.3.1 / XFCE
Size: 1.2GB
Last Modified: 2025-08-05 14:30:22
```

### Create and Activate Sessions
```
$ minios-session-cli create --mode dynfilefs
Session 4 created successfully (mode: dynfilefs)

$ minios-session-cli activate 4
Session 4 activated (was session 1)
```

### Safe Deletion
```
$ minios-session-cli delete 1
Error: Cannot delete currently active session

$ minios-session-cli delete 2
Session 2 deleted successfully
```

## Integration with MiniOS

This tool integrates with the MiniOS boot system that manages sessions through:
- Boot parameters: `perchdir=resume|new|ask|N`
- Session persistence modes: `perchmode=native|dynfilefs|raw`
- Automatic session detection and management

## Privilege Management

The session manager uses PolicyKit (pkexec) to handle privilege escalation when needed:

### Permission Levels

- **Read Operations**: List sessions, view current session, show filesystem info
  - Usually require no special privileges
  - May request authentication if session directory is restricted
  
- **Write Operations**: Create, activate, delete sessions
  - Automatically request user authentication via PolicyKit
  - Use policy `dev.minios.session-manager.write`
  
- **Administrative Operations**: Cleanup operations, system-level changes
  - Require administrator authentication
  - Use policy `dev.minios.session-manager.admin`

### PolicyKit Policies

Three permission levels are defined:

1. **dev.minios.session-manager.read**: View sessions (usually no auth required)
2. **dev.minios.session-manager.write**: Modify sessions (user authentication) 
3. **dev.minios.session-manager.admin**: Administrative operations (admin authentication)

### User Experience

- Programs start without requiring root privileges
- Authentication is requested only when needed
- Clear error messages for authentication failures
- Operations are atomic - either succeed with proper permissions or fail safely

## Development

### Project Structure
```
minios-session-manager/
├── bin/                    # Executable scripts
│   ├── minios-session-manager     # GUI launcher
│   └── minios-session-cli         # CLI launcher
├── lib/                    # Python modules
│   ├── session_manager.py         # GUI application
│   └── session_cli.py             # CLI application
├── share/                  # Shared files
│   ├── applications/       # Desktop entries
│   └── polkit/            # PolicyKit configurations
├── debian/                 # Debian packaging
├── po/                     # Translations (future)
├── Makefile               # Build system
└── README.md              # This file
```

### Testing

The tool can be tested in a development environment by:
1. Creating a test sessions directory: `/tmp/changes`
2. Adding test session directories: `/tmp/changes/1`, `/tmp/changes/2`
3. Creating metadata files: `session.json` or `session.conf`

## License

GPL-3.0+ - See LICENSE file for details.

## Contributing

Contributions are welcome! Please submit issues and pull requests to the main MiniOS repository.

## Authors

MiniOS Team <team@minios.dev>