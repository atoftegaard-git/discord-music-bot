#!/bin/bash

# This script automates committing and pushing changes using a Gemini-generated commit message.
# It automatically analyzes all changes to tracked files, generates a commit message,
# commits, and pushes.
#
# Usage:
# ./git-commit-ai.sh

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Untracked Files Check ---
# Ensures you don't forget to 'git add' new files before running.
UNTRACKED_FILES=$(git ls-files --others --exclude-standard)
if [ -n "$UNTRACKED_FILES" ]; then
  echo "Error: Found untracked files. Please stage them manually with 'git add' before committing."
  echo "Untracked files:"
  echo "$UNTRACKED_FILES"
  exit 1
fi

# 1. Get the diff of all tracked files
echo "🔎 Analyzing changes to all tracked files..."
# We use 'git diff HEAD' to see all changes that 'git commit -a' would commit.
DIFF=$(git diff HEAD)

# If no changes, inform the user and exit.
if [ -z "$DIFF" ]; then
  echo "✅ No changes to tracked files to commit."
  exit 0
fi

# 2. Generate a commit message using Gemini
echo "🤖 Generating commit message from Gemini..."

# Construct the detailed prompt for Gemini
GEMINI_PROMPT="Write a git commit message in the Conventional Commits format based on the following diff.

Rules:
1. Subject line: Must start with one of these exact types: fix, feat, build, chore, ci, docs, style, refactor, perf, test.
2. Max 72 characters for the subject line.
3. Body: A single, concise paragraph explaining the 'why' of the changes.
4. STRICTLY PLAIN TEXT. Do not use markdown, no backticks, no code blocks, no lists.

Diff:
$DIFF"

# Call the gemini command and store the output
COMMIT_MSG=$(gemini -m gemini-2.5-flash "$GEMINI_PROMPT" | sed 's/`//g')

# Check if Gemini returned a message
if [ -z "$COMMIT_MSG" ]; then
    echo "Error: Failed to generate commit message from Gemini. Aborting."
    exit 1
fi

echo "💬 Commit message generated:"
echo "--------------------------"
echo "$COMMIT_MSG"
echo "--------------------------"
echo ""

# 3. Stage all tracked changes and commit with the generated message
echo "🔨 Staging and committing all tracked changes..."
git commit -a -F - <<< "$COMMIT_MSG"

# 4. Push the commit
echo "🚀 Pushing changes to remote..."
git push

echo "✅ Done!"
