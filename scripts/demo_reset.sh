#!/bin/bash
# Returns the demo to a clean state by calling the admin reset endpoint.
set -euo pipefail
cd "$(dirname "$0")/.."
URL=$(cat .dashboard_url 2>/dev/null || echo "${DASHBOARD_URL:?set .dashboard_url}")
AT=$(grep '^ADMIN_TOKEN=' .env.local 2>/dev/null | cut -d= -f2-)
curl -s -X POST "$URL/api/reset" -H "x-boardagents-token: ${AT:-}" -H 'content-type: application/json' -d '{}'
echo
echo "dashboard: $URL"
