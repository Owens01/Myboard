#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
sam delete --stack-name "${STACK:-boardagents}" --no-prompts
