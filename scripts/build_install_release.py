"""Build a reproducible, allowlisted self-host release and a single fresh-install stack."""

import argparse
import copy
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

from cfnlint.decode import decode

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    data, errors = decode(str(ROOT / name))
    if errors:
        raise ValueError("invalid_template_source")
    return copy.deepcopy(data)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def template(base_url, bundle_hash, bootstrap_hash):
    runtime = load("infra/runtime.yaml")
    analysis = load("infra/analysis.yaml")
    result = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "L5PHA private investor. Fresh installation in Sydney. "
            "Observation only until owner activation."
        ),
        "Parameters": {},
        "Conditions": {},
        "Resources": {},
        "Outputs": {},
    }
    parameters = result["Parameters"]
    parameters.update(
        {
            "ImageId": {
                "Type": "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>",
                "Default": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
            },
            "InstanceType": {
                "Type": "String",
                "Default": "t3.small",
                "AllowedValues": ["t3.small", "t3.medium"],
            },
            "InvitationHash": {
                "Type": "String",
                "AllowedPattern": "[a-f0-9]{64}",
                "Description": (
                    "Auto-filled by installer. Only a SHA-256 hash; "
                    "never paste a broker or model key."
                ),
            },
            "DeploymentId": {"Type": "String", "AllowedPattern": "[a-f0-9]{32}"},
            "BotId": {"Type": "Number", "Default": 8808983730, "MinValue": 1},
            "SearchGatewayArn": {
                "Type": "String",
                "Default": "",
                "AllowedPattern": (
                    "^$|^arn:aws:bedrock-agentcore:ap-northeast-1:[0-9]{12}:gateway/[a-z0-9-]+$"
                ),
                "Description": (
                    "Optional advanced Bedrock search. OpenAI/Gemini "
                    "use their own search without this."
                ),
            },
            "SearchGatewayUrl": {
                "Type": "String",
                "Default": "",
                "AllowedPattern": r"^$|^https://[a-z0-9-]+\.gateway\.bedrock-agentcore\.ap-northeast-1\.amazonaws\.com/mcp$",
            },
            "ModelConfigurationRevision": {
                "Type": "String",
                "Default": "0.26.1",
                "AllowedPattern": "[A-Za-z0-9._-]+",
                "MaxLength": 64,
            },
        }
    )
    result["Rules"] = {
        "SydneyOnly": {
            "Assertions": [
                {
                    "Assert": {"Fn::Equals": [{"Ref": "AWS::Region"}, "ap-southeast-2"]},
                    "AssertDescription": "This release is verified for Sydney (ap-southeast-2).",
                }
            ]
        }
    }
    result["Conditions"]["HasSearch"] = {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "SearchGatewayArn"}, ""]}]
    }

    def convert(value):
        if isinstance(value, dict):
            if value == {"Ref": "AnalysisFunctionArn"}:
                return {"Ref": "AnalysisAlias"}
            if "Fn::If" in value and value["Fn::If"][0] in {
                "AnalysisConfigured",
                "BootstrapSecret",
            }:
                return convert(value["Fn::If"][1])
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v) for v in value]
        return value

    resources = convert(runtime["Resources"])
    resources.update(analysis["Resources"])
    statements = resources["AnalysisRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    resources["AnalysisRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"] = [
        {"Fn::If": ["HasSearch", s, {"Ref": "AWS::NoValue"}]}
        if s.get("Sid") in {"InvokePublicSearchOnly", "DenyOtherGateways"}
        else s
        for s in statements
    ]
    for name in ["BrokerSecret", "DataVolume"]:
        resources[name]["DeletionPolicy"] = "RetainExceptOnCreate"
    resources["DataVolume"]["Properties"]["AvailabilityZone"] = {
        "Fn::Select": [0, {"Fn::GetAZs": ""}]
    }
    resources["DataVolume"]["Properties"]["Size"] = 16
    resources["Host"]["Properties"]["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] = 16
    resources["Host"]["Properties"]["Tags"] = [
        {"Key": "Name", "Value": {"Fn::Sub": "${AWS::StackName}-L5PHA"}}
    ]
    resources["InstallationReady"] = {
        "Type": "AWS::CloudFormation::WaitCondition",
        "DependsOn": ["AddressAssociation", "DataAttachment"],
        "CreationPolicy": {"ResourceSignal": {"Count": 1, "Timeout": "PT25M"}},
    }
    role_statements = resources["HostRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    for s in role_statements:
        if s.get("Sid") == "ExplicitlyDenyOtherAwsActions":
            s["NotAction"].append("cloudformation:SignalResource")
    release_url = urlsplit(base_url)
    bucket = release_url.hostname.split(".s3.")[0]
    prefix = release_url.path.strip("/")
    for statement in role_statements:
        if statement.get("Sid") == "ExplicitlyDenyOtherAwsActions":
            statement["NotAction"].append("s3:GetObject")
    role_statements.append(
        {
            "Sid": "PinnedReleaseFiles",
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": [
                f"arn:aws:s3:::{bucket}/{prefix}/runtime.tar.gz",
                f"arn:aws:s3:::{bucket}/{prefix}/bootstrap_install.py",
            ],
        }
    )
    role_statements.append(
        {
            "Sid": "SignalOwnInstallation",
            "Effect": "Allow",
            "Action": "cloudformation:SignalResource",
            "Resource": {"Ref": "AWS::StackId"},
        }
    )
    config = {
        "ip": "${Address}",
        "bot_id": "${BotId}",
        "secret_arn": "${BrokerSecret}",
        "analysis_arn": "${AnalysisAlias}",
        "volume_id": "${DataVolume}",
        "invitation_hash": "${InvitationHash}",
        "deployment_id": "${DeploymentId}",
        "version": "0.26.1",
        "search_gateway_url": "${SearchGatewayUrl}",
        "bundle_url": base_url + "/runtime.tar.gz",
        "bundle_sha256": bundle_hash,
    }
    script = (
        """#!/bin/bash
set -eu
umask 077
install -d -m 0755 /opt/veyquant
cat > /etc/l5pha-bootstrap.json <<'L5PHA_CONFIG'
"""
        + json.dumps(config)
        + """
L5PHA_CONFIG
status=FAILURE
finish() {
 aws cloudformation signal-resource --stack-name '${AWS::StackId}' \
 --logical-resource-id InstallationReady --unique-id '${DeploymentId}' --status "$status" \
 --region '${AWS::Region}' >/dev/null
}
trap finish EXIT
for attempt in $(seq 1 90); do
 actual_ip=$(curl -fsS --connect-timeout 3 --max-time 5 https://checkip.amazonaws.com || true)
 if [ "$actual_ip" = '${Address}' ]; then break; fi
 sleep 2
done
[ "$actual_ip" = '${Address}' ]
dnf install -y python3.13 python3.13-pip >/dev/null
python3.13 - <<'L5PHA_FETCH'
import subprocess, hashlib
from pathlib import Path
subprocess.run(["aws", "s3", "cp","""
        + repr("s3://" + bucket + "/" + prefix + "/bootstrap_install.py")
        + """, "/opt/veyquant/bootstrap.py", "--only-show-errors"], check=True, timeout=60)
data=Path('/opt/veyquant/bootstrap.py').read_bytes()
assert hashlib.sha256(data).hexdigest()=="""
        + repr(bootstrap_hash)
        + """
Path('/opt/veyquant/bootstrap.py').write_bytes(data)
L5PHA_FETCH
python3.13 /opt/veyquant/bootstrap.py
status=SUCCESS
"""
    )
    resources["Host"]["Properties"]["UserData"] = {"Fn::Base64": {"Fn::Sub": script}}
    resources["Recovery"] = {
        "Type": "AWS::SSM::Document",
        "Properties": {
            "DocumentType": "Command",
            "Content": {
                "schemaVersion": "2.2",
                "description": "L5PHA AWS-owner recovery. No broker orders are sent.",
                "parameters": {
                    "Action": {
                        "type": "String",
                        "default": "pause",
                        "allowedValues": [
                            "pause",
                            "revoke-sessions",
                            "new-code",
                            "disconnect-telegram",
                        ],
                        "interpolationType": "ENV_VAR",
                    }
                },
                "mainSteps": [
                    {
                        "action": "aws:runShellScript",
                        "name": "Recover",
                        "inputs": {
                            "runCommand": [
                                "export PYTHONPATH=/opt/veyquant/runtime/src",
                                "runuser -u veyweb -- /opt/veyquant/venv/bin/python "
                                "/opt/veyquant/runtime/scripts/recover_installation.py "
                                '"$SSM_Action"',
                            ]
                        },
                    }
                ],
            },
        },
    }
    result["Resources"] = resources
    result["Outputs"] = {
        "OpenTelegram": {
            "Description": (
                "Open after CREATE_COMPLETE. Keep the one-time code from the installer."
            ),
            "Value": {
                "Fn::Join": [
                    "",
                    [
                        "https://t.me/veyquant_bot?start=l5_",
                        {"Fn::Join": ["_", {"Fn::Split": [".", {"Ref": "Address"}]}]},
                        "_",
                        {"Ref": "DeploymentId"},
                    ],
                ]
            },
        },
        "PrivateDashboard": {"Value": {"Fn::Sub": "https://${Address}"}},
        "TossAllowedIP": {
            "Description": "Register this exact outbound IP in Toss Open API.",
            "Value": {"Ref": "Address"},
        },
        "RecoveryConsole": {
            "Description": "AWS-only emergency access. Does not depend on Telegram.",
            "Value": {
                "Fn::Sub": "https://${AWS::Region}.console.aws.amazon.com/systems-manager/session-manager/${Host}?region=${AWS::Region}"
            },
        },
        "DataVolumeToKeepOrDelete": {"Value": {"Ref": "DataVolume"}},
        "SecretToKeepOrDelete": {"Value": {"Ref": "BrokerSecret"}},
        "ProviderKeyToKeepOrDelete": {"Value": {"Ref": "ProviderKey"}},
    }
    result["Outputs"]["RecoveryActions"] = {
        "Description": "Select the action and this instance in AWS Run Command.",
        "Value": {
            "Fn::Sub": "https://${AWS::Region}.console.aws.amazon.com/systems-manager/run-command/send-command?region=${AWS::Region}#documentName=${Recovery}"
        },
    }
    result["Metadata"] = {
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {
                    "Label": {"default": "Installation identity (filled automatically)"},
                    "Parameters": ["InvitationHash", "DeploymentId"],
                },
                {
                    "Label": {"default": "Advanced options (normally unchanged)"},
                    "Parameters": [
                        "InstanceType",
                        "ImageId",
                        "BotId",
                        "SearchGatewayArn",
                        "SearchGatewayUrl",
                        "ModelConfigurationRevision",
                    ],
                },
            ]
        }
    }
    return result


def build(output, base_url):
    output.mkdir(parents=True, exist_ok=True)
    selected = []
    for folder in ["src/veyquant", "deploy"]:
        selected += [
            p
            for p in (ROOT / folder).rglob("*")
            if p.is_file()
            and p.name != "l5pha-access.service"
            and p.suffix in {".py", ".html", ".css", ".js", ".service", ".timer", ".conf", ".lock"}
        ]
    selected.append(ROOT / "scripts/recover_installation.py")
    manifest = {
        "version": "0.26.1",
        "files": {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in sorted(selected)},
    }
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        for p in sorted(selected):
            value = p.read_bytes()
            info = tarfile.TarInfo(str(p.relative_to(ROOT)))
            info.size = len(value)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(value))
        value = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        info = tarfile.TarInfo("release-manifest.json")
        info.size = len(value)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(value))
    bundle = gzip.compress(data.getvalue(), mtime=0)
    bootstrap = (ROOT / "scripts/bootstrap_install.py").read_bytes()
    (output / "runtime.tar.gz").write_bytes(bundle)
    (output / "bootstrap_install.py").write_bytes(bootstrap)
    stack = template(base_url, sha(bundle), sha(bootstrap))
    (output / "install.json").write_text(json.dumps(stack, indent=2) + "\n")
    manifest = {
        "version": "0.26.1",
        "region": "ap-southeast-2",
        "bot_username": "veyquant_bot",
        "template_url": base_url + "/install.json",
        "files": {
            name: sha((output / name).read_bytes())
            for name in ["runtime.tar.gz", "bootstrap_install.py", "install.json"]
        },
    }
    (output / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "version": manifest["version"],
                "runtime_files": len(selected),
                "bundle_bytes": len(bundle),
                "template_bytes": (output / "install.json").stat().st_size,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    if not args.base_url.startswith("https://") or any(c in args.base_url for c in "'\"\n$"):
        parser.error("HTTPS release prefix required")
    build(Path(args.output), args.base_url.rstrip("/"))


if __name__ == "__main__":
    main()
