"""One-time operator provisioning: ciphertext to exactly one AWS secret; no plaintext output."""

import json
import logging
import os
import resource
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def main():
    logging.disable(logging.CRITICAL)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    directory = Path(sys.argv[1])
    secret_arn = sys.argv[2]
    if (
        directory.parent != Path("/run")
        or not directory.name.startswith("veyquant-probe.")
        or directory.is_symlink()
    ):
        raise SystemExit("invalid_directory")
    success = False
    reason = "provisioning_failed"
    try:
        os.rename(directory / "key.pem", directory / "consumed-key.pem")
        result = subprocess.run(
            [
                "openssl",
                "cms",
                "-decrypt",
                "-binary",
                "-inform",
                "DER",
                "-in",
                str(directory / "envelope.der"),
                "-inkey",
                str(directory / "consumed-key.pem"),
                "-recip",
                str(directory / "cert.pem"),
            ],
            capture_output=True,
            timeout=15,
        )
        (directory / "consumed-key.pem").unlink()
        if result.returncode:
            raise ValueError
        payload = json.loads(result.stdout)
        if payload["version"] != 1 or not time.time() < payload["expires_at"] <= time.time() + 660:
            raise ValueError
        keys = payload["credentials"]
        if set(keys) != {"TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ"}:
            raise ValueError
        client = boto3.Session(region_name="ap-southeast-2").client(
            "secretsmanager",
            config=Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 1}),
        )
        client.put_secret_value(
            SecretId=secret_arn, ClientRequestToken=str(uuid.uuid4()), SecretString=json.dumps(keys)
        )
        success = True
    except ClientError:
        reason = "aws_request_failed"
    except Exception:
        pass
    finally:
        shutil.rmtree(directory)
    print(
        json.dumps(
            {
                "secret_stored": success,
                "reason": "stored" if success else reason,
                "temporary_material_removed": not directory.exists(),
            }
        )
    )
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
