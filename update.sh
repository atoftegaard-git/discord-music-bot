#!/bin/bash

# This script automates the process of updating and restarting the Docker container.
# It brings down the running services, pulls the latest changes from git,
# and then rebuilds and restarts the services in detached mode.

# Exit immediately if a command exits with a non-zero status.
set -e

echo "INFO: Bringing down existing containers..."
docker-compose down

echo "INFO: Pulling latest changes from git..."
git pull

echo "INFO: Building and starting new containers..."
docker-compose up -d --build

echo "INFO: Update complete!"
