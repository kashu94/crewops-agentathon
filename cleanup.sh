#!/bin/bash
set -euo pipefail

# =============================================================================
# Crew Ops Agent-a-thon — Cleanup
# Deletes the resource group created by challenge-0-setup/deploy.sh
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "No .env file found at $ENV_FILE — nothing to clean up."
    exit 0
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

if [ -z "${RESOURCE_GROUP:-}" ]; then
    echo "RESOURCE_GROUP not set in .env — nothing to clean up."
    exit 0
fi

echo "This will delete resource group: $RESOURCE_GROUP"
read -p "Are you sure? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

az group delete --name "$RESOURCE_GROUP" --yes --no-wait
echo "Deletion of $RESOURCE_GROUP started (running in the background)."
