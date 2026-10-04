#!/bin/sh
# Run on the authenticated SSM target only. Outputs the public certificate, never its key.
set -eu
umask 077
probe_dir=$(mktemp -d /run/veyquant-probe.XXXXXXXX)
trap 'rm -rf -- "$probe_dir"' EXIT HUP INT TERM
openssl req -x509 -newkey rsa:3072 -nodes -days 1 \
  -subj /CN=veyquant-one-time-probe \
  -keyout "$probe_dir/key.pem" -out "$probe_dir/cert.pem" >/dev/null 2>&1
# Bound key lifetime even if the operator disconnects before sending the envelope.
systemd-run --quiet --on-active=15m /usr/bin/rm -rf -- "$probe_dir"
printf '%s\n' "$probe_dir"
cat "$probe_dir/cert.pem"
trap - EXIT HUP INT TERM
