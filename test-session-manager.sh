#!/bin/bash
#
# Test script for MiniOS Session Manager
# Creates test environment and demonstrates functionality
#

set -e

TEST_DIR="/tmp/minios-session-manager-test"
CHANGES_DIR="$TEST_DIR/changes"

echo "Setting up test environment..."

# Clean up any existing test
rm -rf "$TEST_DIR"
mkdir -p "$CHANGES_DIR"

# Create test sessions
mkdir -p "$CHANGES_DIR"/{1,2,3}

# Create some test files in sessions
echo "test data 1" > "$CHANGES_DIR/1/testfile1.txt"
echo "test data 2" > "$CHANGES_DIR/2/testfile2.txt" 
mkdir -p "$CHANGES_DIR/3/subdir"
echo "test data 3" > "$CHANGES_DIR/3/subdir/testfile3.txt"

# Create test session metadata (JSON format)
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
    },
    "3": {
      "mode": "raw",
      "version": "3.2.0",
      "edition": "Flux"
    }
  }
}
EOF

# Make session directories have different modification times
touch -d "2025-08-01" "$CHANGES_DIR/3"
touch -d "2025-07-15" "$CHANGES_DIR/2"  
touch -d "2025-08-05" "$CHANGES_DIR/1"

echo "Test environment created at: $TEST_DIR"
echo "Sessions directory: $CHANGES_DIR"
echo ""

# Temporarily modify the session CLI to use our test directory
SCRIPT_DIR=$(dirname "$(readlink -f -- "$0")")
TEST_CLI="$TEST_DIR/session_cli_test.py"

# Create test version that uses our test directory
sed "s|/run/initramfs/changes|$CHANGES_DIR|g; s|/mnt/live/changes|$CHANGES_DIR|g; s|/live/changes|$CHANGES_DIR|g" \
    "$SCRIPT_DIR/lib/session_cli.py" > "$TEST_CLI"

echo "Running tests..."
echo "=================="

echo "1. List sessions:"
python3 "$TEST_CLI" list
echo ""

echo "2. Show current session:"
python3 "$TEST_CLI" current
echo ""

echo "3. Attempting to delete current session (should fail):"
python3 "$TEST_CLI" delete 1 || echo "Expected failure - cannot delete current session"
echo ""

echo "4. Delete session 3:"
python3 "$TEST_CLI" delete 3
echo ""

echo "5. List sessions after deletion:"
python3 "$TEST_CLI" list
echo ""

echo "6. Cleanup test (should delete session 2 if older than threshold):"
python3 "$TEST_CLI" cleanup --days 15
echo ""

echo "7. Final session list:"
python3 "$TEST_CLI" list
echo ""

echo "Test completed successfully!"
echo ""
echo "To test CLI directly:"
echo "  python3 '$TEST_CLI' list"
echo ""
echo "To test GUI mode (requires GTK3):"
echo "  Create a test GUI launcher that uses the test CLI"
echo ""
echo "To clean up test environment:"
echo "  rm -rf '$TEST_DIR'"