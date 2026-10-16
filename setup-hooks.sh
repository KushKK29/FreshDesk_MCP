#!/bin/sh
# Installs the local pre-push hook that runs check.py before every push.
# .git/hooks/ isn't tracked by git, so run this once per clone.
cp "$(dirname "$0")/pre-push.hook" "$(git rev-parse --git-path hooks)/pre-push"
chmod +x "$(git rev-parse --git-path hooks)/pre-push"
echo "Installed: git push now runs check.py first."
