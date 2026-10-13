#!/bin/bash
# Deploys Boardagents. Reads BEDROCK_API_KEY / ADMIN_TOKEN from .env.local if present.
set -euo pipefail
cd "$(dirname "$0")/.."
STACK="${STACK:-boardagents}"
PROFILE="${AWS_PROFILE:-sai}"
export AWS_PROFILE="$PROFILE"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-south-1}"
ARGS=(--resolve-s3 --capabilities CAPABILITY_NAMED_IAM --stack-name "$STACK")
# Secrets live in .env (or .env.local); both are gitignored. Never committed.
ENVFILE="${ENVFILE:-}"
[ -z "$ENVFILE" ] && [ -f .env.local ] && ENVFILE=.env.local
[ -z "$ENVFILE" ] && [ -f .env ] && ENVFILE=.env
if [ -n "$ENVFILE" ]; then
  echo "reading secrets from $ENVFILE"
  BK=$(grep '^BEDROCK_API_KEY=' "$ENVFILE" | cut -d= -f2-)
  AT=$(grep '^ADMIN_TOKEN=' "$ENVFILE" | cut -d= -f2-)
  [ -n "${BK:-}" ] && ARGS+=(--parameter-overrides BedrockApiKey="$BK" AdminToken="${AT:-$(openssl rand -hex 16)}" UseFakeLlm=false)
else
  echo "WARNING: no .env/.env.local found - deploying with USE_FAKE_LLM=true"
fi
PATH="/opt/homebrew/bin:$PATH"
# Context manifest + prompts must live inside CodeUri (src/) to reach the Lambdas.
python3 scripts/build_context.py
mkdir -p src/prompts && cp prompts/*.md src/prompts/
echo "prompts bundled: $(ls src/prompts | tr '\n' ' ')"
sam build
sam deploy "${ARGS[@]}"
URL=$(aws cloudformation describe-stacks --stack-name "$STACK" --profile "$PROFILE" --query 'Stacks[0].Outputs[?OutputKey==`DashboardUrl`].OutputValue' --output text)
echo
echo "Dashboard: $URL"
echo "$URL" > .dashboard_url
