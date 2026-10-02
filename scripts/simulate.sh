#!/usr/bin/env bash
# Inject failures into the managed instance through SSM (no SSH or open ports needed).
#   ./scripts/simulate.sh cpu        runaway process, as in the 2025 demo (`yes > /dev/null`)
#   ./scripts/simulate.sh cpu-java   runaway process that is NOT on the kill allow-list -> reboot
#   ./scripts/simulate.sh hang       break guest networking -> instance status check fails -> reboot
#   ./scripts/simulate.sh replace    (ASG mode) terminate the instance -> ASG launches a replacement
# System status-check failures happen on AWS hardware and cannot be injected by customers;
# see docs/runbook.md for how that path is verified.
set -euo pipefail
STACK="${STACK_NAME:-self-healing-ec2}"
out() { aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
IID="$(out InstanceId)"
if [ -z "$IID" ] || [ "$IID" = "None" ]; then
  ASG="$(out AutoScalingGroupName)"
  IID="$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" \
        --query 'AutoScalingGroups[0].Instances[0].InstanceId' --output text)"
fi
run() { aws ssm send-command --instance-ids "$IID" --document-name AWS-RunShellScript \
          --parameters "commands=[\"$1\"]" --query Command.CommandId --output text; }
case "${1:-}" in
  cpu)      run "nohup sh -c 'for i in \$(seq \$(nproc)); do yes > /dev/null & done' >/dev/null 2>&1 &" ;;
  cpu-java) run "cp /usr/bin/python3 /tmp/java && for i in \$(seq \$(nproc)); do nohup /tmp/java -c 'while True: pass' >/dev/null 2>&1 & done" ;;
  hang)     run "nohup sh -c 'sleep 5; ip link set \$(ip route | awk \"/default/ {print \\\$5}\") down' >/dev/null 2>&1 &" ;;
  replace)  aws ec2 terminate-instances --instance-ids "$IID" --query 'TerminatingInstances[0].CurrentState.Name' ;;
  *) echo "usage: simulate.sh cpu|cpu-java|hang|replace"; exit 2 ;;
esac
echo "Injected '${1}' into $IID. Watch: aws cloudwatch describe-alarms --alarm-name-prefix $STACK"
