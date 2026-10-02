"""Level-2 remediation: diagnose a high-CPU instance, then fix it as gently as possible.

Triggered by EventBridge when the stack's sustained-high-CPU alarm enters ALARM.

1. Work out which instance(s) the alarm refers to (instance alarm or ASG alarm).
2. Respect a per-instance cooldown so a misbehaving workload cannot cause a reboot loop.
3. Ask the instance, over SSM Run Command, which processes are using the CPU.
4. If the top process is on the allow-list of known-killable workloads, kill just that
   process; otherwise (or if diagnostics fail) reboot the instance.
5. Tag the instance with what was done and e-mail a summary through SNS.

Every external call goes through ``Remediator``'s boto3 clients so the logic can be
unit-tested with moto (see tests/).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import boto3

TAG_LAST = "self-healing:last-remediation"
TAG_ACTION = "self-healing:last-action"
DIAG_SCRIPT = "ps -eo pid=,comm=,pcpu= --sort=-pcpu | head -n 5"


@dataclass
class Decision:
    instance_id: str
    action: str                      # "kill-process" | "reboot" | "skip-cooldown"
    reason: str
    top_processes: list = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_top(output: str) -> list[dict]:
    """Parse `ps -eo pid=,comm=,pcpu=` output into dicts, highest CPU first."""
    rows = []
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            try:
                rows.append({"pid": int(parts[0]), "name": parts[1], "cpu": float(parts[-1])})
            except ValueError:
                continue
    return rows


def choose_action(top: list[dict], killable: set[str]) -> tuple[str, str]:
    if not top:
        return "reboot", "diagnostics returned no process list"
    worst = top[0]
    if worst["name"] in killable:
        return "kill-process", f"'{worst['name']}' (pid {worst['pid']}) is using {worst['cpu']:.0f}% CPU and is on the kill allow-list"
    return "reboot", f"top process '{worst['name']}' ({worst['cpu']:.0f}% CPU) is not on the kill allow-list"


class Remediator:
    def __init__(self, ec2=None, ssm=None, sns=None, autoscaling=None, *, topic_arn: str,
                 cooldown_minutes: float = 30, killable: set[str] | None = None,
                 poll_seconds: float = 2.0, max_wait_seconds: float = 60.0):
        self.ec2 = ec2 or boto3.client("ec2")
        self.ssm = ssm or boto3.client("ssm")
        self.sns = sns or boto3.client("sns")
        self.autoscaling = autoscaling or boto3.client("autoscaling")
        self.topic_arn = topic_arn
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.killable = killable or set()
        self.poll_seconds, self.max_wait = poll_seconds, max_wait_seconds

    # ---------------------------------------------------------------- targets
    def instances_for_alarm(self, event: dict) -> list[str]:
        dims = {}
        for m in event.get("detail", {}).get("configuration", {}).get("metrics", []):
            dims.update(m.get("metricStat", {}).get("metric", {}).get("dimensions", {}))
        if "InstanceId" in dims:
            return [dims["InstanceId"]]
        if "AutoScalingGroupName" in dims:
            groups = self.autoscaling.describe_auto_scaling_groups(
                AutoScalingGroupNames=[dims["AutoScalingGroupName"]])["AutoScalingGroups"]
            return [i["InstanceId"] for g in groups for i in g["Instances"]
                    if i.get("LifecycleState") == "InService"]
        raise ValueError(f"alarm has no InstanceId/AutoScalingGroupName dimension: {dims}")

    def _tags(self, instance_id: str) -> dict:
        res = self.ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
        tags = res[0]["Instances"][0].get("Tags", []) if res else []
        return {t["Key"]: t["Value"] for t in tags}

    def in_cooldown(self, instance_id: str) -> bool:
        last = self._tags(instance_id).get(TAG_LAST)
        return bool(last) and _now() - datetime.fromisoformat(last) < self.cooldown

    # ---------------------------------------------------------------- SSM
    def run(self, instance_id: str, script: str) -> str | None:
        cmd = self.ssm.send_command(InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
                                    Parameters={"commands": [script]}, TimeoutSeconds=60)
        cid, waited = cmd["Command"]["CommandId"], 0.0
        while waited <= self.max_wait:
            try:
                inv = self.ssm.get_command_invocation(CommandId=cid, InstanceId=instance_id)
            except self.ssm.exceptions.InvocationDoesNotExist:
                inv = {"Status": "Pending"}
            if inv["Status"] == "Success":
                return inv.get("StandardOutputContent", "")
            if inv["Status"] in ("Failed", "Cancelled", "TimedOut"):
                return None
            time.sleep(self.poll_seconds)
            waited += self.poll_seconds
        return None

    # ---------------------------------------------------------------- main
    def decide(self, instance_id: str) -> Decision:
        if self.in_cooldown(instance_id):
            return Decision(instance_id, "skip-cooldown",
                            f"remediated less than {self.cooldown} ago; leaving it to the status-check alarms")
        output = self.run(instance_id, DIAG_SCRIPT)
        top = parse_top(output) if output is not None else []
        action, reason = choose_action(top, self.killable)
        if output is None:
            reason = "SSM diagnostics failed or timed out"
        return Decision(instance_id, action, reason, top)

    def act(self, d: Decision) -> None:
        if d.action == "kill-process":
            self.run(d.instance_id, f"kill -9 {d.top_processes[0]['pid']}")
        elif d.action == "reboot":
            self.ec2.reboot_instances(InstanceIds=[d.instance_id])
        if d.action != "skip-cooldown":
            self.ec2.create_tags(Resources=[d.instance_id], Tags=[
                {"Key": TAG_LAST, "Value": _now().isoformat()},
                {"Key": TAG_ACTION, "Value": d.action}])

    def notify(self, alarm: str, decisions: list[Decision]) -> None:
        lines = [f"Alarm: {alarm}", ""]
        for d in decisions:
            lines += [f"Instance {d.instance_id}: {d.action}", f"  why: {d.reason}"]
            for p in d.top_processes[:5]:
                lines.append(f"  {p['pid']:>7}  {p['name']:<20} {p['cpu']:5.1f}% CPU")
        self.sns.publish(TopicArn=self.topic_arn, Subject=f"Self-healing: {alarm}"[:100],
                         Message="\n".join(lines))

    def handle(self, event: dict) -> list[Decision]:
        alarm = event.get("detail", {}).get("alarmName", "unknown alarm")
        decisions = []
        for iid in self.instances_for_alarm(event):
            d = self.decide(iid)
            self.act(d)
            decisions.append(d)
        self.notify(alarm, decisions)
        return decisions


def handler(event, context=None):
    r = Remediator(
        topic_arn=os.environ["TOPIC_ARN"],
        cooldown_minutes=float(os.environ.get("COOLDOWN_MINUTES", "30")),
        killable={p.strip() for p in os.environ.get("KILLABLE_PROCESSES", "").split(",") if p.strip()},
    )
    decisions = r.handle(event)
    out = [d.__dict__ for d in decisions]
    print(json.dumps(out))
    return out
