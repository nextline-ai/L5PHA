"""Fresh EC2 installation only. Called by pinned CloudFormation UserData, not a shell wizard."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path("/opt/veyquant/runtime")
DATA = Path("/var/lib/veyquant")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, timeout=kwargs.pop("timeout", 180), **kwargs)


def fetch(url, digest, target):
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or not url.startswith("https://"):
        raise ValueError("invalid_release")
    parsed = urlsplit(url)
    if not re.fullmatch(r"[a-z0-9.-]+\.s3\.ap-southeast-2\.amazonaws\.com", parsed.hostname or ""):
        raise ValueError("invalid_release_host")
    bucket = parsed.hostname.split(".s3.")[0]
    run(
        "aws",
        "s3",
        "cp",
        "s3://" + bucket + parsed.path,
        str(target),
        "--only-show-errors",
        timeout=120,
    )
    data = target.read_bytes()
    if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("release_integrity_failed")
    target.write_bytes(data)


def mount_data(volume):
    # Never guess /dev/nvme1n1: match the exact newly-created CF volume serial.
    wanted = volume.replace("-", "")
    for _ in range(90):
        devices = json.loads(
            run(
                "lsblk",
                "--json",
                "--output",
                "NAME,SERIAL,TYPE,FSTYPE",
                capture_output=True,
                text=True,
            ).stdout
        )["blockdevices"]
        match = [
            d for d in devices if d.get("serial", "").strip() == wanted and d["type"] == "disk"
        ]
        if match:
            break
        time.sleep(2)
    else:
        raise ValueError("data_volume_not_attached")
    disk = match[0]
    if disk.get("fstype") or disk.get("children"):
        raise ValueError("data_volume_not_empty_use_recovery")
    device = "/dev/" + disk["name"]
    run("mkfs.xfs", device)
    DATA.mkdir(parents=True, exist_ok=True)
    uuid = run(
        "blkid", "-s", "UUID", "-o", "value", device, capture_output=True, text=True
    ).stdout.strip()
    if not re.fullmatch(r"[a-fA-F0-9-]{36}", uuid):
        raise ValueError("invalid_volume_uuid")
    with Path("/etc/fstab").open("a") as f:
        f.write(f"\nUUID={uuid} /var/lib/veyquant xfs defaults,nofail 0 2\n")
    run("mount", str(DATA))


def main():
    stage = "packages"
    try:
        config = json.loads(Path("/etc/l5pha-bootstrap.json").read_text())
        run(
            "dnf", "install", "-y", "python3.13", "python3.13-pip", "nginx", "xfsprogs", timeout=420
        )
        stage = "release"
        archive = Path("/opt/l5pha-release.tar.gz")
        fetch(config["bundle_url"], config["bundle_sha256"], archive)
        ROOT.mkdir(parents=True, exist_ok=False)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                p = Path(member.name)
                if p.is_absolute() or ".." in p.parts or not member.isfile():
                    raise ValueError("invalid_release_archive")
            tar.extractall(ROOT, filter="data")
        stage = "data_volume"
        mount_data(config["volume_id"])
        (DATA / "deployment-id").write_text(config["deployment_id"])
        stage = "dependencies"
        run("python3.13", "-m", "venv", "/opt/veyquant/venv")
        run(
            "/opt/veyquant/venv/bin/pip",
            "install",
            "--quiet",
            "--require-hashes",
            "-r",
            str(ROOT / "deploy/requirements.lock"),
            timeout=420,
        )
        run("python3.13", "-m", "venv", "/opt/veyquant-certbot")
        run("/opt/veyquant-certbot/bin/pip", "install", "--quiet", "certbot==5.8.0", timeout=180)
        # UserData uses umask 077 for configuration. Shared code must remain traversable.
        for tree in (ROOT, Path("/opt/veyquant/venv")):
            tree.chmod(0o755)
            for directory in tree.rglob("*"):
                if directory.is_dir():
                    directory.chmod(0o755)
                elif directory.is_file():
                    directory.chmod(0o755 if directory.stat().st_mode & 0o111 else 0o644)
        stage = "users"
        for group in [
            "veystatus",
            "veyapi",
            "veyaccount",
            "veyexecution",
            "veyprovider",
            "veyresearch",
        ]:
            run("groupadd", "--system", group)
        for user, group, extra in [
            ("veyweb", "veyapi", "veystatus,veyaccount,veyprovider,veyresearch"),
            ("veycollector", "veystatus", "veyexecution,veyresearch"),
            ("veyanalysis", "veystatus", "veyresearch"),
            ("veyprovider", "veyprovider", ""),
        ]:
            args = [
                "useradd",
                "--system",
                "--gid",
                group,
                "--no-create-home",
                "--shell",
                "/sbin/nologin",
            ]
            if extra:
                args += ["--groups", extra]
            run(*args, user)
        run("usermod", "-a", "-G", "veyapi", "nginx")
        for folder, user, group, mode in [
            ("control", "veyweb", "veystatus", "2750"),
            ("reports", "veyanalysis", "veystatus", "0750"),
            ("analysis", "veyanalysis", "veystatus", "0700"),
            ("management", "veyweb", "veyapi", "0700"),
            ("collector", "veycollector", "veystatus", "0700"),
            ("status", "veycollector", "veystatus", "0750"),
            ("account-view", "veycollector", "veyaccount", "2750"),
            ("execution-control", "veyweb", "veyexecution", "2750"),
            ("execution", "veycollector", "veystatus", "0700"),
            ("setup", "root", "root", "0755"),
        ]:
            run("install", "-d", "-o", user, "-g", group, "-m", mode, str(DATA / folder))
        run("install", "-d", "-m", "0700", "/var/lib/veyquant-secrets")
        run("install", "-d", "-m", "0755", "/var/lib/veyquant-acme")
        substitutions = {
            "IP": config["ip"],
            "BOT_ID": str(config["bot_id"]),
            "SECRET_ARN": config["secret_arn"],
            "ANALYSIS_ARN": config["analysis_arn"],
        }
        for source in (ROOT / "deploy").iterdir():
            if (
                source.suffix not in {".service", ".timer"}
                or source.name == "veyquant-credentials.service"
            ):
                continue
            text = source.read_text()
            for key, value in substitutions.items():
                text = text.replace("@" + key + "@", value)
            text = text.replace(
                "After=network.target aws-workload-credentials-provider-token.service",
                "After=network.target",
            ).replace("Requires=aws-workload-credentials-provider-token.service\n", "")
            text = text.replace(
                "After=network-online.target veyquant-credentials.service",
                "After=network-online.target",
            ).replace(
                "Requires=veyquant-credentials.service",
                "ConditionPathExists=/var/lib/veyquant-secrets/toss.cred",
            )
            if not config.get("search_gateway_url") and source.name in {
                "veyquant-management.service",
                "veyquant-analysis.service",
            }:
                text = text.replace(
                    "[Service]", "[Service]\nEnvironment=L5PHA_DIRECT_PROVIDERS_ONLY=true"
                )
            text = text.replace(" /var/run/awssmatoken", " -/var/run/awssmatoken")
            target = Path("/etc/systemd/system") / source.name
            target.write_text(text)
            target.chmod(0o644)
        Path("/etc/veyquant-installation.json").write_text(
            json.dumps({k: config[k] for k in ["ip", "bot_id", "version", "deployment_id"]})
        )
        Path("/etc/veyquant-installation.json").chmod(0o644)
        stage = "https"
        Path("/etc/nginx/nginx.conf").write_text(
            "events {}\nhttp { server { listen 80; location /.well-known/acme-challenge/ "
            "{ root /var/lib/veyquant-acme; } location / { return 503; } } }\n"
        )
        run("systemctl", "enable", "--now", "nginx")
        for attempt in range(6):
            try:
                if (
                    urlopen("https://checkip.amazonaws.com", timeout=10).read().decode().strip()
                    != config["ip"]
                ):
                    raise ValueError("egress_not_ready")
                run(
                    "/opt/veyquant-certbot/bin/certbot",
                    "certonly",
                    "--non-interactive",
                    "--agree-tos",
                    "--register-unsafely-without-email",
                    "--webroot",
                    "-w",
                    "/var/lib/veyquant-acme",
                    "--ip-address",
                    config["ip"],
                    "--preferred-profile",
                    "shortlived",
                    "--cert-name",
                    "veyquant-ip",
                    timeout=120,
                )
                break
            except (ValueError, OSError, subprocess.SubprocessError):
                if attempt == 5:
                    raise
                time.sleep(15)
        nginx = (ROOT / "deploy/nginx.conf").read_text().replace("@IP@", config["ip"])
        nginx = nginx.replace(
            "location = /v1/providers/connect {",
            "location ~ ^/v1/(providers/connect|installation/broker)$ {",
        )
        Path("/etc/nginx/nginx.conf").write_text(nginx)
        stage = "owner_invitation"
        env = os.environ | {"PYTHONPATH": str(ROOT / "src")}
        run(
            "runuser",
            "-u",
            "veyweb",
            "--",
            "/opt/veyquant/venv/bin/python",
            "-m",
            "veyquant.cli",
            "install-invitation",
            "--db",
            str(DATA / "management/state.sqlite3"),
            "--bot-id",
            str(config["bot_id"]),
            "--hash",
            config["invitation_hash"],
            env=env,
        )
        stage = "services"
        run("systemctl", "daemon-reload")
        run(
            "systemctl",
            "enable",
            "--now",
            "veyquant-setup",
            "veyquant-management",
            "veyquant-provider",
            "veyquant-analysis",
            "veyquant-certificate.timer",
        )
        run("systemctl", "enable", "veyquant-collector")
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
        for _ in range(20):
            try:
                if urlopen("https://" + config["ip"] + "/healthz", timeout=5).status == 200:
                    break
            except OSError:
                time.sleep(2)
        else:
            raise ValueError("https_health_failed")
        stage = "ready"
        print(json.dumps({"installation": "ready", "live_enabled": False}))
        return 0
    except Exception as error:
        # No key-bearing exceptions or shell traces. Stage plus CF events drive recovery UX.
        print(
            json.dumps(
                {"installation": "failed", "stage": stage, "error_type": type(error).__name__}
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
