#!/bin/bash
#
# Update translation files for MiniOS Session Manager
#

set -e

# Extract translatable strings from Python source
xgettext --keyword=_ --language=Python --add-comments --sort-output \
    --output=po/messages.pot lib/session_manager.py lib/session_cli.py

echo "Updated po/messages.pot"

# Update existing .po files
for po in po/*.po; do
    if [ -f "$po" ]; then
        echo "Updating $po"
        msgmerge --update "$po" po/messages.pot
    fi
done

echo "Translation update complete"