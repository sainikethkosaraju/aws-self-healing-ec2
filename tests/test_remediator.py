"""Unit tests for the remediation Lambda against moto-simulated EC2, SSM, SNS and Auto Scaling."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lambda"))
import remediator as rem  # noqa: E402

REGION = "ap-southeast-2"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
        iid = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1, TagSpecifications=[
            {"ResourceType": "instance", "Tags": [{"Key": "SelfHealing", "Value": "managed"}]}])["Instances"][0]["InstanceId"]
        topic = boto3.client("sns", region_name=REGION).create_topic(Name="alerts")["TopicArn"]
        yield {"ec2": ec2, "iid": iid, "topic": topic}


def alarm_event(dims):
    return {"source": "aws.cloudwatch", "detail-type": "CloudWatch Alarm State Change",
            "detail": {"alarmName": "shec2-sustained-high-cpu", "state": {"value": "ALARM"},
                       "configuration": {"metrics": [{"metricStat": {"metric": {
                           "namespace": "AWS/EC2", "name": "CPUUtilization", "dimensions": dims}}}]}}}


def make(env, diag_output, **kw):
    r = rem.Remediator(topic_arn=env["topic"], killable={"yes", "stress-ng"}, poll_seconds=0, **kw)
    calls = []

    def fake_run(instance_id, script):
        calls.append(script)
        return diag_output if script == rem.DIAG_SCRIPT else ""
    r.run = fake_run
    return r, calls


def tags(env):
    inst = env["ec2"].describe_instances(InstanceIds=[env["iid"]])["Reservations"][0]["Instances"][0]
    return {t["Key"]: t["Value"] for t in inst["Tags"]}


def test_parse_top_and_policy():
    top = rem.parse_top("  3625 yes   99.7\n   812 python3  0.3\n garbage line\n")
    assert top[0] == {"pid": 3625, "name": "yes", "cpu": 99.7} and len(top) == 2
    assert rem.choose_action(top, {"yes"})[0] == "kill-process"
    assert rem.choose_action(top, set())[0] == "reboot"
    assert rem.choose_action([], {"yes"})[0] == "reboot"


def test_kills_allow_listed_runaway_process(env):
    # the exact failure the 2025 demo injected: `yes > /dev/null &`
    r, calls = make(env, "3625 yes 99.7\n812 python3 0.3\n")
    [d] = r.handle(alarm_event({"InstanceId": env["iid"]}))
    assert d.action == "kill-process"
    assert calls == [rem.DIAG_SCRIPT, "kill -9 3625"]
    assert tags(env)[rem.TAG_ACTION] == "kill-process"


def test_reboots_when_culprit_unknown(env, monkeypatch):
    rebooted = []
    r, _ = make(env, "4100 java 98.0\n")
    monkeypatch.setattr(r.ec2, "reboot_instances", lambda **kw: rebooted.append(kw["InstanceIds"]))
    [d] = r.handle(alarm_event({"InstanceId": env["iid"]}))
    assert d.action == "reboot" and rebooted == [[env["iid"]]]


def test_reboots_when_diagnostics_fail(env, monkeypatch):
    r, _ = make(env, None)
    monkeypatch.setattr(r.ec2, "reboot_instances", lambda **kw: None)
    [d] = r.handle(alarm_event({"InstanceId": env["iid"]}))
    assert d.action == "reboot" and "SSM" in d.reason


def test_cooldown_prevents_reboot_loops(env):
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    env["ec2"].create_tags(Resources=[env["iid"]], Tags=[{"Key": rem.TAG_LAST, "Value": recent}])
    r, calls = make(env, "3625 yes 99.7\n", cooldown_minutes=30)
    [d] = r.handle(alarm_event({"InstanceId": env["iid"]}))
    assert d.action == "skip-cooldown" and calls == []


def test_resolves_instances_behind_an_asg(env):
    asg = boto3.client("autoscaling", region_name=REGION)
    ami = env["ec2"].describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    asg.create_launch_configuration(LaunchConfigurationName="lc", ImageId=ami, InstanceType="t3.micro")
    asg.create_auto_scaling_group(AutoScalingGroupName="g", LaunchConfigurationName="lc", MinSize=1, MaxSize=1,
                                  AvailabilityZones=[f"{REGION}a"])
    r, _ = make(env, "3625 yes 99.7\n")
    ids = r.instances_for_alarm(alarm_event({"AutoScalingGroupName": "g"}))
    assert len(ids) == 1 and ids[0].startswith("i-")


def test_handler_publishes_summary(env, monkeypatch):
    sent = []
    monkeypatch.setenv("TOPIC_ARN", env["topic"])
    monkeypatch.setenv("KILLABLE_PROCESSES", "yes, stress-ng")
    monkeypatch.setattr(rem.Remediator, "run",
                        lambda self, iid, script: "3625 yes 99.7\n" if script == rem.DIAG_SCRIPT else "")
    real_publish = boto3.client("sns", region_name=REGION).publish
    monkeypatch.setattr(rem.Remediator, "notify", lambda self, alarm, ds: sent.append((alarm, ds)) or
                        real_publish(TopicArn=env["topic"], Message="x"))
    out = rem.handler(alarm_event({"InstanceId": env["iid"]}))
    assert out[0]["action"] == "kill-process" and sent[0][0] == "shec2-sustained-high-cpu"


def test_unknown_alarm_shape_is_an_error(env):
    r, _ = make(env, "")
    with pytest.raises(ValueError):
        r.instances_for_alarm(alarm_event({"Foo": "bar"}))
