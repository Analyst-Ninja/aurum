#!/bin/sh
# SSM port-forward to the AURUM MCP host: localhost:8000 -> instance:8000.
# Looks the instance up by its Name tag (the ID changes whenever Terraform replaces it) and
# reconnects whenever the session drops (idle timeout, sleep, network change).
set -u

REGION="${AWS_REGION:-us-east-1}"
PORT="${MCP_PORT:-8000}"

while true; do
  ID=$(aws ec2 describe-instances --region "$REGION" \
    --filters Name=tag:Name,Values=aurum-mcp Name=instance-state-name,Values=running \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null)

  if [ -n "$ID" ] && [ "$ID" != "None" ]; then
    aws ssm start-session --region "$REGION" --target "$ID" \
      --document-name AWS-StartPortForwardingSession \
      --parameters "{\"portNumber\":[\"$PORT\"],\"localPortNumber\":[\"$PORT\"]}"
  else
    echo "no running aurum-mcp instance found; retrying" >&2
  fi
  sleep 5
done
