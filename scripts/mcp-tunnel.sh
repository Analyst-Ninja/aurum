#!/bin/sh
# Keeps an SSM port-forward from localhost:8000 to the aurum MCP EC2 host alive.
# Needs: AWS CLI + Session Manager plugin, and AWS credentials for the account.
# Reconnects when the session drops (SSM idles out) or the instance is replaced.
REGION="${AWS_REGION:-us-east-1}"
PORT="${AURUM_MCP_LOCAL_PORT:-8000}"

while true; do
  ID=$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=aurum-mcp" "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[0].InstanceId' --output text)
  if [ -n "$ID" ] && [ "$ID" != "None" ]; then
    aws ssm start-session --region "$REGION" --target "$ID" \
      --document-name AWS-StartPortForwardingSession \
      --parameters "portNumber=8000,localPortNumber=$PORT"
  else
    echo "no running aurum-mcp instance found" >&2
  fi
  sleep 5
done
