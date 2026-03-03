#!/bin/bash

# This script automates the process of updating and restarting the Docker container.
# It brings down the running services, pulls the latest changes from git,
# and then rebuilds and restarts the services in detached mode.

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

echo "INFO: Bringing down existing containers..."
$COMPOSE_CMD down

echo "INFO: Pulling latest changes from git..."
git pull

echo "INFO: Building and starting new containers..."
$COMPOSE_CMD up -d --build

echo "INFO: Update complete!"
