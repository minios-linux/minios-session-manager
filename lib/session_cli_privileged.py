#!/usr/bin/env python3
"""
MiniOS Session CLI Privileged Wrapper

This script runs with elevated privileges and performs session operations
that require system-level access. It's called via pkexec from the main CLI.
"""

import sys
import os
import json
import subprocess
import argparse

# Add the lib directory to Python path to import session_cli
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from session_cli import SessionManager

def main():
    """Main entry point for privileged operations"""
    parser = argparse.ArgumentParser(description='MiniOS Session Manager Privileged Operations')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # List command (read operation)
    list_parser = subparsers.add_parser('list', help='List all sessions')
    
    # Current command (read operation)
    current_parser = subparsers.add_parser('current', help='Show current session')
    
    # Info command (read operation)
    info_parser = subparsers.add_parser('info', help='Show filesystem and compatibility information')
    
    # Activate command (write operation)
    activate_parser = subparsers.add_parser('activate', help='Activate a session')
    activate_parser.add_argument('session_id', help='Session ID to activate')
    
    # Create command (write operation)
    create_parser = subparsers.add_parser('create', help='Create a new session')
    create_parser.add_argument('--mode', choices=['native', 'dynfilefs', 'raw'], 
                              default='native', help='Session mode (default: native)')
    create_parser.add_argument('--size', type=int, metavar='MB',
                              help='Size in MB for dynfilefs/raw modes (default: 4000)')
    
    # Delete command (write operation)
    delete_parser = subparsers.add_parser('delete', help='Delete a session')
    delete_parser.add_argument('session_id', help='Session ID to delete')
    
    # Cleanup command (admin operation)
    cleanup_parser = subparsers.add_parser('cleanup', help='Clean up old sessions')
    cleanup_parser.add_argument('--days', type=int, default=30, 
                               help='Delete sessions older than N days (default: 30)')
    
    args = parser.parse_args()
    
    # Initialize session manager
    manager = SessionManager()
    
    if not manager.sessions_dir:
        print("Error: Could not find sessions directory.", file=sys.stderr)
        print("This tool must be run from within a MiniOS live system with persistent sessions enabled.", file=sys.stderr)
        sys.exit(1)
    
    # Import format_session_list function
    from session_cli import format_session_list
    
    # Handle commands - same logic as original CLI but running with privileges
    if args.command == 'list':
        sessions = manager.list_sessions()
        print(format_session_list(sessions))
    
    elif args.command == 'current':
        current = manager.get_current_session()
        if current:
            print(f"Current session: #{current['id']}")
            print(f"Mode: {current['mode']}")
            print(f"Version: {current['version']} / {current['edition']}")
            print(f"Size: {manager._format_size(current['size'])}")
            print(f"Last Modified: {current['modified'].strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            print("No current session found")
    
    elif args.command == 'info':
        fs_info, error = manager.get_filesystem_info()
        if error:
            print(f"Error: {error}", file=sys.stderr)
            sys.exit(1)
        
        print("MiniOS Media Information:")
        print("-" * 40)
        fs = fs_info['filesystem']
        print(f"Filesystem Type: {fs['type']}")
        print(f"Device: {fs['device']}")
        print(f"Mount Options: {fs['mount_options'] or 'none'}")
        print(f"Read-only: {'Yes' if fs['is_readonly'] else 'No'}")
        print(f"POSIX Compatible: {'Yes' if fs['is_posix_compatible'] else 'No'}")
        print()
        
        print("Compatible Session Modes:")
        compatible = fs_info['compatible_modes']
        if compatible:
            for mode in compatible:
                print(f"  ✓ {mode}")
        else:
            print("  None (read-only media)")
        print()
        
        limitations = fs_info['limitations']
        if limitations:
            print("Filesystem Limitations:")
            if 'max_file_size' in limitations:
                print(f"  • Maximum file size: {limitations['max_file_size']}MB ({limitations['max_file_size'] / 1024:.1f}GB)")
            if 'no_posix' in limitations:
                print("  • No POSIX features (no native mode support)")
            if 'case_insensitive' in limitations:
                print("  • Case-insensitive filenames")
        else:
            print("No known limitations")
    
    elif args.command == 'activate':
        success, message = manager.activate_session(args.session_id)
        print(message)
        sys.exit(0 if success else 1)
    
    elif args.command == 'create':
        success, message = manager.create_session(args.mode, args.size)
        print(message)
        sys.exit(0 if success else 1)
    
    elif args.command == 'delete':
        success, message = manager.delete_session(args.session_id)
        print(message)
        sys.exit(0 if success else 1)
    
    elif args.command == 'cleanup':
        deleted_count, errors = manager.cleanup_old_sessions(args.days)
        print(f"Cleanup completed: {deleted_count} sessions deleted")
        if errors:
            print("Errors:")
            for error in errors:
                print(f"  {error}")
    
    else:
        parser.print_help()

if __name__ == '__main__':
    main()