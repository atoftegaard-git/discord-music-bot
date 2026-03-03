#!/bin/bash

# This script is for quickly updating the bot during development.
# It pulls the latest changes from git, and then rebuilds and restarts the Docker containers.

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Docker Compose Command Detection ---
# Check if 'docker compose' (V2) is available, otherwise fall back to 'docker-compose' (V1)
if docker compose version &>/dev/null; then
    COMPOSE_CMD="docker compose"
    echo "INFO: Using 'docker compose' (V2)"
else
    COMPOSE_CMD="docker-compose"
    echo "INFO: Using 'docker-compose' (V1)"
fi

echo "INFO: Pulling latest changes from git..."
git pull

echo "INFO: Building and starting containers..."
$COMPOSE_CMD up --build

echo "INFO: Development update complete! Bot should be running with latest changes."
