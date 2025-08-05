# MiniOS Session Manager

A utility suite for managing MiniOS persistent sessions from within the running system.

## Components

This package provides two complementary tools:

* **minios-session**: Command-line interface for session operations.
* **minios-session-manager**: GTK3 graphical interface that uses the CLI utility.

## Features

* **List Sessions**: View all available sessions with detailed information (size, version, last modified).
* **Current Session**: Display information about the actively active session.
* **Create Sessions**: Create new sessions with choice of persistence mode (native, dynfilefs, raw).
* **Activate Sessions**: Switch the default session for next boot.
* **Delete Sessions**: Remove old or unused sessions (cannot delete active session).
* **Cleanup**: Automatically remove sessions older than specified days.
* **All Session Types**: Full support for native, dynfilefs, and raw persistence modes.
* **Dual Interface**: Both command-line and graphical user interfaces.
* **Format Support**: Compatible with both JSON and legacy session configuration formats.
* **Safety**: Prevents deletion of actively active sessions.
* **Privilege Management**: Automatic privilege escalation when needed using PolicyKit/pkexec.
* **User-Friendly**: Runs without root privileges, requests authentication only when necessary.

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
minios-session list

# Show active session
minios-session active

# Create new session with native mode (default)
minios-session create

# Create new session with dynfilefs mode
minios-session create --mode dynfilefs

# Create new session with raw mode
minios-session create --mode raw

# Activate session #2
minios-session activate 2

# Delete a specific session
minios-session delete 3

# Clean up sessions older than 30 days
minios-session cleanup --days 30
```

### Graphical Interface

Launch the GUI from:

* Application menu: `System` → `MiniOS Session Manager`
* Command line: `minios-session-manager`

## License

GPL-3.0+ - See LICENSE file for details.

## Author

crims0n <crims0n@minios.dev>