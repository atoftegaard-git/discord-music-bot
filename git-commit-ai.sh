#!/bin/bash

# This script automates committing and pushing changes using a Gemini-generated commit message.
#
# Usage:
# ./git-commit-ai.sh "Your description of the changes"

# Exit immediately if a command exits with a non-zero status.
set -e

export GOOGLE_CLOUD_PROJECT="netic-code-assist"
# Only run on standard commits (no merge, squash, or amend)
if [ -z "$2" ]; then

  # Check if gemini is installed
  if ! command -v gemini &> /dev/null; then
    echo "⚠️ gemini-cli not found. Skipping AI message generation." >&2
    exit 0
  fi

  echo "🤖 Gemini is analyzing your changes..." >&2

  # Get the staged diff
  DIFF=$(git diff --cached)

  # If diff is empty, exit
  if [ -z "$DIFF" ]; then
    exit 0
  fi

  # Construct the prompt
  PROMPT="Write a git commit message in the Conventional Commits format based on this diff.

Rules:
1. Subject line: Must start with one of these exact types: fix, feat, build, chore, ci, docs, style, refactor, perf, test.
2. Max 50 characters for the subject line.
3. Body: A single, very concise paragraph explaining the 'why'.
4. STRICTLY PLAIN TEXT. Do not use markdown, do not use code blocks (no backticks), do not use lists.

Diff:
$DIFF"

  # Call gemini-cli
  MSG=$(gemini -m gemini-2.5-flash "$PROMPT" | sed 's/`//g')

  # Prepend the generated message to the commit file
  # We use a temp file to prepend without overwriting existing comments/templates
  if [ -n "$MSG" ]; then
      echo "$MSG" | cat - "$1" > temp && mv temp "$1"
  fi
fi

# 2. Generate a commit message using Gemini
echo "🤖 Generating commit message from Gemini..."

# Call the gemini command and store the output
COMMIT_MSG=$(gemini "$PROMPT")

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

# 3. Stage all tracked changes and commit
echo "🔨 Staging and committing all changes..."
git commit -a -m "$COMMIT_MSG"

# 4. Push the commit
echo "🚀 Pushing changes to remote..."
git push

rm temp
echo "✅ Done!"

