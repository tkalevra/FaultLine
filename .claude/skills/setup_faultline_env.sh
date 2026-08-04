#!/bin/bash
# FaultLine Environment Setup Helper
# Sets environment variables needed for reset-faultline-db skill
#
# Usage:
#   source ./.claude/skills/setup_faultline_env.sh
#   /skill reset-faultline-db
#
# OR run standalone to verify current settings:
#   bash ./.claude/skills/setup_faultline_env.sh

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SKILLS_DIR="$REPO_DIR/.claude/skills"
CONFIG_FILE="$SKILLS_DIR/.faultline_config.json"

# Colors for output
GREEN='\033[92m'
BLUE='\033[94m'
YELLOW='\033[93m'
RED='\033[91m'
RESET='\033[0m'

log_info() { echo -e "${BLUE}ℹ${RESET} $1"; }
log_success() { echo -e "${GREEN}✓${RESET} $1"; }
log_warning() { echo -e "${YELLOW}⚠${RESET} $1"; }
log_error() { echo -e "${RED}✗${RESET} $1"; }

# Default values (adjust as needed for your environment)
DEFAULT_FAULTLINE_URL="${FAULTLINE_URL:-http://localhost:8000}"
DEFAULT_FAULTLINE_TOKEN="${FAULTLINE_TOKEN:-}"
DEFAULT_FAULTLINE_USER_ID="${FAULTLINE_USER_ID:-00000000-0000-0000-0000-000000000000}"
DEFAULT_POSTGRES_DSN="${POSTGRES_DSN:-postgresql://faultline:faultline@localhost:5432/faultline_test}"
DEFAULT_QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"

# Load from saved config if available
if [[ -f "$CONFIG_FILE" ]]; then
    log_info "Loading configuration from $CONFIG_FILE"
    if command -v jq &>/dev/null; then
        DEFAULT_FAULTLINE_URL="$(jq -r '.FAULTLINE_URL // empty' "$CONFIG_FILE")" || true
        DEFAULT_FAULTLINE_USER_ID="$(jq -r '.FAULTLINE_USER_ID // empty' "$CONFIG_FILE")" || true
        DEFAULT_QDRANT_URL="$(jq -r '.QDRANT_URL // empty' "$CONFIG_FILE")" || true
    fi
fi

# Set environment variables
export FAULTLINE_URL="${DEFAULT_FAULTLINE_URL}"
export FAULTLINE_USER_ID="${DEFAULT_FAULTLINE_USER_ID}"
export POSTGRES_DSN="${DEFAULT_POSTGRES_DSN}"
export QDRANT_URL="${DEFAULT_QDRANT_URL}"

# Token requires manual input (shouldn't be saved to file)
if [[ -z "$FAULTLINE_TOKEN" ]]; then
    log_warning "FAULTLINE_TOKEN not set"
    echo "Enter your OpenWebUI bearer token (sk-...):"
    read -r -s FAULTLINE_TOKEN
    export FAULTLINE_TOKEN
    echo ""
fi

# Verify configuration
echo ""
log_info "Environment Configuration:"
echo "  FAULTLINE_URL=$FAULTLINE_URL"
echo "  FAULTLINE_USER_ID=$FAULTLINE_USER_ID"
echo "  POSTGRES_DSN=$POSTGRES_DSN (user:pass@host:port/db)"
echo "  QDRANT_URL=$QDRANT_URL"
echo ""

# Validate PostgreSQL DSN format
if [[ ! "$POSTGRES_DSN" =~ postgresql:// ]]; then
    log_error "POSTGRES_DSN format invalid. Expected: postgresql://user:pass@host:port/dbname"
    exit 1
fi

log_success "Environment variables configured"
echo ""
echo "Ready to run: /skill reset-faultline-db"
echo ""
