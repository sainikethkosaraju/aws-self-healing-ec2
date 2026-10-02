# Runbook: deploying and verifying the self-healing paths

> **Status of these procedures.** The template passes `cfn-lint` against the AWS resource
> specification, and the remediation logic is unit-tested against simulated AWS APIs (moto).
> The rebuilt stack has **not yet been deployed to a live account**, because the original
> student account no longer exists. Each test below lists what you should observe. Record
> the actual outcome (screenshots, timings) in `docs/evidence/` when you run it.

## Prerequisites

* AWS CLI v2 configured for an account where you can create IAM roles (`CAPABILITY_IAM`).
* A region whose AMI parameter `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64` exists (all commercial regions).
* An e-mail inbox for the alerts.

```bash
./scripts/deploy.sh you@example.com                       # single instance
./scripts/deploy.sh you@example.com auto-scaling-group    # or: replaced-by-ASG mode
```

Confirm the SNS subscription e-mail. Until you do, no alerts arrive.

## Test 1: runaway process is killed (Level 2, allow-listed)

```bash
./scripts/simulate.sh cpu
```

Expected after about `CpuEvaluationMinutes` (default 5) minutes:
1. `self-healing-ec2-sustained-high-cpu` goes to ALARM, and an alert e-mail arrives.
2. EventBridge invokes `self-healing-ec2-remediator`. Its CloudWatch Logs show the `ps` output.
3. The `yes` process is killed (only the top one, so with several cores the alarm may re-fire
   once the cooldown expires).
4. A "Self-healing: …" e-mail lists the action, and the instance gets the tags
   `self-healing:last-action=kill-process` and `self-healing:last-remediation=<time>`.
5. The alarm returns to OK.

## Test 2: unknown CPU hog leads to a reboot (Level 2, fallback)

```bash
./scripts/simulate.sh cpu-java
```

The runaway process is called `java`, which is not on the allow-list, so the remediator reboots
the instance. The tag `self-healing:last-action=reboot` is set. Running it again within the
cooldown (default 30 min) produces `skip-cooldown` instead of a second reboot.

## Test 3: guest OS failure leads to a reboot (Level 1)

```bash
./scripts/simulate.sh hang
```

Taking the primary network interface down makes the **instance** status check fail (the
hypervisor can no longer reach the guest). After about 3 minutes, `instance-status-check`
goes to ALARM and the native `ec2:reboot` action restarts the instance, which brings the
interface back.

## Test 4: hardware failure leads to auto-recovery (Level 1)

System status-check failures happen on AWS's side (host hardware, power, network) and cannot
be caused by a customer. To check the wiring without a real failure:

```bash
aws cloudwatch set-alarm-state --alarm-name self-healing-ec2-system-status-check \
  --state-value ALARM --state-reason "manual test of alarm actions"
```

This proves the SNS notification path. AWS may not run the `recover` action for a
synthetic state change on a healthy instance. Its behaviour on real hardware faults is
documented by AWS: the instance moves to new hardware and keeps its ID, private IP, EBS
volumes and metadata.

## Test 5: instance replacement (ASG mode)

```bash
./scripts/simulate.sh replace
```

The ASG notices the terminated instance, launches a replacement from the same launch template,
and sends `EC2_INSTANCE_TERMINATE` and `EC2_INSTANCE_LAUNCH` notifications to the topic.

## Tear down

```bash
./scripts/destroy.sh
```
