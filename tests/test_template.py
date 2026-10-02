"""Contract tests for template.yaml.

`cfn-lint` (run in CI) validates every resource against the AWS resource specification.
These tests check the *behaviour* the template promises: which metric drives which
action, what is scoped to which deployment mode, and how far the IAM role reaches.

(moto's CloudFormation engine does not implement CloudWatch alarms or SNS subscriptions,
so deploying the stack into moto would only test the parts that matter least.)
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        val = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        val = loader.construct_sequence(node, deep=True)
    else:
        val = loader.construct_mapping(node, deep=True)
    if suffix == "GetAtt" and isinstance(val, str):
        val = val.split(".", 1)
    return {("Ref" if suffix == "Ref" else f"Fn::{suffix}"): val}


_CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def t():
    return yaml.load((ROOT / "template.yaml").read_text(), Loader=_CfnLoader)


def res(t, name):
    return t["Resources"][name]


def props(t, name):
    return res(t, name)["Properties"]


def actions(p):
    """Flatten AlarmActions into readable strings."""
    out = []
    for a in p["AlarmActions"]:
        if "Ref" in a:
            out.append(f"ref:{a['Ref']}")
        else:
            out.append(a["Fn::Sub"])
    return out


def test_system_check_triggers_native_recovery(t):
    p = props(t, "SystemStatusAlarm")
    assert p["MetricName"] == "StatusCheckFailed_System" and p["Namespace"] == "AWS/EC2"
    assert any(a.endswith(":ec2:recover") for a in actions(p))
    assert "ref:AlertTopic" in actions(p)
    assert res(t, "SystemStatusAlarm")["Condition"] == "IsSingleInstance"


def test_instance_check_triggers_reboot(t):
    p = props(t, "InstanceStatusAlarm")
    assert p["MetricName"] == "StatusCheckFailed_Instance"
    assert any(a.endswith(":ec2:reboot") for a in actions(p))
    assert p["EvaluationPeriods"] >= 2          # don't reboot on a single noisy datapoint


def test_recover_is_never_wired_to_cpu(t):
    """EC2 auto-recovery is only valid on StatusCheckFailed_System (the 2025 build's misconception)."""
    for name, r in t["Resources"].items():
        if r["Type"] == "AWS::CloudWatch::Alarm" and any(a.endswith(":ec2:recover") for a in actions(r["Properties"]) if "ref:" not in a):
            assert r["Properties"]["MetricName"] == "StatusCheckFailed_System", name


def test_cpu_alarm_only_notifies_and_routes_to_lambda(t):
    p = props(t, "HighCpuAlarm")
    assert actions(p) == ["ref:AlertTopic"]     # no blind reboot on CPU
    assert p["DatapointsToAlarm"] == {"Ref": "CpuEvaluationMinutes"}
    dim = p["Dimensions"][0]["Fn::If"]
    assert dim[0] == "IsSingleInstance"
    assert dim[1]["Name"] == "InstanceId" and dim[2]["Name"] == "AutoScalingGroupName"
    rule = props(t, "HighCpuRule")
    assert rule["EventPattern"]["detail"]["alarmName"] == [{"Ref": "HighCpuAlarm"}]
    assert rule["EventPattern"]["detail"]["state"]["value"] == ["ALARM"]
    assert rule["Targets"][0]["Arn"] == {"Fn::GetAtt": ["RemediationFunction", "Arn"]}
    perm = props(t, "HighCpuRulePermission")
    assert perm["Principal"] == "events.amazonaws.com"


def test_deployment_modes_are_exclusive(t):
    assert res(t, "Instance")["Condition"] == "IsSingleInstance"
    assert res(t, "AutoScalingGroup")["Condition"] == "IsAutoScaling"
    asg = props(t, "AutoScalingGroup")
    assert (asg["MinSize"], asg["MaxSize"], asg["HealthCheckType"]) == ("1", "1", "EC2")


def test_iam_least_privilege(t):
    stmts = props(t, "RemediationRole")["Policies"][0]["PolicyDocument"]["Statement"]
    by_sid = {s["Sid"]: s for s in stmts}
    act = by_sid["ActOnlyOnInstancesTaggedByThisStack"]
    assert set(act["Action"]) == {"ec2:RebootInstances", "ec2:CreateTags", "ssm:SendCommand"}
    assert act["Condition"]["StringEquals"]["aws:ResourceTag/SelfHealing"] == "managed"
    assert by_sid["Notify"]["Resource"] == {"Ref": "AlertTopic"}
    # nothing may terminate or stop instances
    flat = [a for s in stmts for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
    assert not any(a in flat for a in ("ec2:TerminateInstances", "ec2:StopInstances", "ec2:*", "*"))


def test_instances_are_hardened_and_tagged(t):
    lt = props(t, "LaunchTemplate")["LaunchTemplateData"]
    assert lt["MetadataOptions"]["HttpTokens"] == "required"
    assert lt["Monitoring"]["Enabled"] is True
    tags = lt["TagSpecifications"][0]["Tags"]
    assert {"Key": "SelfHealing", "Value": "managed"} in tags
    assert "SecurityGroupIngress" not in props(t, "InstanceSecurityGroup")   # no open ports


def test_lambda_wiring(t):
    p = props(t, "RemediationFunction")
    assert p["Handler"] == "remediator.handler" and p["Runtime"].startswith("python3")
    env = p["Environment"]["Variables"]
    assert set(env) == {"TOPIC_ARN", "COOLDOWN_MINUTES", "KILLABLE_PROCESSES"}
    assert (ROOT / "lambda" / "remediator.py").exists()
