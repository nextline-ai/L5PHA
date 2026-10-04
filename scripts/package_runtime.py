"""Bundle public runtime files and the skill-provided asm-exec, never local config."""

import argparse
import base64
import hashlib
import json
import zlib
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asm-exec", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    files = {}
    for folder in ("src/veyquant", "scripts", "deploy"):
        for path in sorted((root / folder).rglob("*")):
            if path.is_file() and path.suffix in {
                ".py",
                ".sh",
                ".html",
                ".css",
                ".js",
                ".conf",
                ".service",
                ".timer",
            }:
                files[str(path.relative_to(root))] = path.read_text()
    wrapper = Path(args.asm_exec).read_text()
    original_hash = hashlib.sha256(wrapper.encode()).hexdigest()
    old = "result = subprocess.run(args)"
    if wrapper.count(old) != 1:
        raise SystemExit("asm-exec source changed; review the environment substitution patch")
    wrapper = wrapper.replace(
        old,
        "runtime_env = {k: resolve_string(v) if PATTERN.search(v) else v "
        "for k, v in os.environ.items()}\n"
        "    result = subprocess.run(args, env=runtime_env)",
    )
    files["asm-exec"] = wrapper
    files["asm-exec-provenance.json"] = json.dumps(
        {
            "source": "AWS Secrets Manager skill references/asm-exec",
            "original_sha256": original_hash,
            "installed_sha256": hashlib.sha256(wrapper.encode()).hexdigest(),
            "change": "Resolve exported environment dynamic references before subprocess launch.",
        }
    )
    payload = base64.b64encode(zlib.compress(json.dumps(files).encode(), 9)).decode()
    Path(args.output).write_text(payload)
    print(
        json.dumps({"files": len(files), "encoded_bytes": len(payload), "contains_env_file": False})
    )


if __name__ == "__main__":
    main()
