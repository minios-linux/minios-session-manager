#!/bin/bash
#
# Test script for privilege escalation functionality
# This script tests that the CLI properly handles privilege escalation
#

set -e

TEST_DIR="/tmp/minios-session-manager-privilege-test"
CHANGES_DIR="$TEST_DIR/changes"

echo "Setting up privilege test environment..."

# Clean up any existing test
rm -rf "$TEST_DIR"
mkdir -p "$CHANGES_DIR"

# Make sessions directory read-only to simulate privilege requirements
mkdir -p "$CHANGES_DIR"/{1,2}
echo "test data 1" > "$CHANGES_DIR/1/testfile1.txt"
echo "test data 2" > "$CHANGES_DIR/2/testfile2.txt"

# Create session metadata
cat > "$CHANGES_DIR/session.json" << 'EOF'
{
  "default": "1",
  "sessions": {
    "1": {
      "mode": "native",
      "version": "3.3.1", 
      "edition": "XFCE"
    },
    "2": {
      "mode": "dynfilefs",
      "version": "3.3.0",
      "edition": "XFCE"
    }
  }
}
EOF

# Make directories owned by root to test privilege escalation
echo "Note: This test requires sudo to simulate privilege requirements"
echo "Making test directories require privileges..."

# Change ownership to root (simulating system directories)
sudo chown -R root:root "$CHANGES_DIR"
sudo chmod -R 755 "$CHANGES_DIR"

# Create test version of CLI
SCRIPT_DIR=$(dirname "$(readlink -f -- "$0")")
TEST_CLI="$TEST_DIR/session_cli_test.py"

# Create test version that uses our test directory
sed "s|/run/initramfs/changes|$CHANGES_DIR|g; s|/mnt/live/changes|$CHANGES_DIR|g; s|/live/changes|$CHANGES_DIR|g" \
    "$SCRIPT_DIR/lib/session_cli.py" > "$TEST_CLI"

echo ""
echo "Running privilege tests..."
echo "========================================="

echo "1. Test list command (should work without privileges):"
python3 "$TEST_CLI" list || echo "Expected: might require privileges"
echo ""

echo "2. Test create command (should request privileges):"
echo "   Note: This should prompt for authentication via pkexec"
echo "   Command: python3 '$TEST_CLI' create --mode native"
echo "   (Run manually to test pkexec integration)"
echo ""

echo "3. Test delete command (should request privileges):"
echo "   Note: This should prompt for authentication via pkexec"
echo "   Command: python3 '$TEST_CLI' delete 2"
echo "   (Run manually to test pkexec integration)"
echo ""

echo "4. Testing privilege detection..."
python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/lib')
from session_cli import SessionManager

manager = SessionManager()
if manager.sessions_dir:
    print(f'Sessions directory: {manager.sessions_dir}')
    has_access = manager._check_access_permissions()
    print(f'Has read access: {has_access}')
    
    needs_privileges = manager._requires_privileges('create')
    print(f'Create operation needs privileges: {needs_privileges}')
    
    needs_privileges = manager._requires_privileges('list')
    print(f'List operation needs privileges: {needs_privileges}')
else:
    print('No sessions directory found')
"

echo ""
echo "Privilege test setup completed!"
echo ""
echo "Manual tests to run:"
echo "1. Test CLI with regular user (should request authentication):"
echo "   python3 '$TEST_CLI' create --mode native"
echo ""
echo "2. Test GUI (should request authentication for write operations):"
echo "   python3 '$SCRIPT_DIR/lib/session_manager.py'"
echo ""
echo "3. Check PolicyKit integration:"
echo "   pkaction --action-id dev.minios.session-manager.write --verbose"
echo ""
echo "To clean up test environment:"
echo "  sudo rm -rf '$TEST_DIR'"
echo ""
echo "Key features to verify:"
echo "✓ Read operations work without privileges (when possible)"
echo "✓ Write operations request authentication via pkexec"
echo "✓ GUI shows proper error messages for authentication failures"
echo "✓ PolicyKit policies are properly configured"