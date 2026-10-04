#!/bin/sh
# After the reviewed stack, data-volume mount, HTTPS certificate and bundle upload.
set -eu
: "${VEYQUANT_IP:?}" "${VEYQUANT_BOT_ID:?}" "${VEYQUANT_SECRET_ARN:?}" "${VEYQUANT_ANALYSIS_ARN:?}"
mountpoint -q /var/lib/veyquant
getent group veystatus >/dev/null || groupadd --system veystatus
getent group veyapi >/dev/null || groupadd --system veyapi
getent group veyaccount >/dev/null || groupadd --system veyaccount
getent group veyexecution >/dev/null || groupadd --system veyexecution
getent group veyprovider >/dev/null || groupadd --system veyprovider
getent group veyresearch >/dev/null || groupadd --system veyresearch
id veyprovider >/dev/null 2>&1 || useradd --system --gid veyprovider --no-create-home --shell /sbin/nologin veyprovider
id veycollector >/dev/null 2>&1 || useradd --system --gid veystatus --no-create-home --shell /sbin/nologin veycollector
id veyweb >/dev/null 2>&1 || useradd --system --gid veyapi --groups veystatus --no-create-home --shell /sbin/nologin veyweb
usermod -a -G veyexecution veycollector
usermod -a -G veyaccount veyweb
usermod -a -G veyprovider veyweb
usermod -a -G veyapi nginx
id veyanalysis >/dev/null 2>&1 || useradd --system --gid veystatus --no-create-home --shell /sbin/nologin veyanalysis
usermod -a -G veyresearch veyanalysis
usermod -a -G veyresearch veycollector
usermod -a -G veyresearch veyweb
install -d -o veyweb -g veystatus -m 2750 /var/lib/veyquant/control
install -d -o veyanalysis -g veystatus -m 0750 /var/lib/veyquant/reports
install -d -o veyanalysis -g veystatus -m 0700 /var/lib/veyquant/analysis
install -d -o veyweb -g veyapi -m 0700 /var/lib/veyquant/management
install -d -o veycollector -g veystatus -m 0700 /var/lib/veyquant/collector
install -d -o veycollector -g veystatus -m 0750 /var/lib/veyquant/status
install -d -o veycollector -g veyaccount -m 2750 /var/lib/veyquant/account-view
install -d -o veyweb -g veyexecution -m 2750 /var/lib/veyquant/execution-control
install -d -o veycollector -g veystatus -m 0700 /var/lib/veyquant/execution
install -d -o root -g root -m 0700 /var/lib/veyquant-secrets
python3.13 -m venv /opt/veyquant/venv
/opt/veyquant/venv/bin/pip install --quiet --require-hashes -r /opt/veyquant-probe-requirements.txt
install -o root -g root -m 0755 /opt/veyquant/runtime/asm-exec /opt/veyquant/asm-exec
python3 - <<'PY'
import os
from pathlib import Path
for source in Path('/opt/veyquant/runtime/deploy').iterdir():
    if source.suffix not in {'.service', '.timer', '.conf'}:
        continue
    target = Path('/etc/nginx/nginx.conf') if source.name == 'nginx.conf' else Path('/etc/systemd/system') / source.name
    text = source.read_text()
    for k in ('IP', 'BOT_ID', 'SECRET_ARN', 'ANALYSIS_ARN'):
        text = text.replace('@' + k + '@', os.environ['VEYQUANT_' + k])
    target.write_text(text)
    target.chmod(0o644)
PY
systemctl daemon-reload
nginx -t
systemctl enable --now veyquant-provider.service veyquant-management.service
systemctl start veyquant-credentials.service
systemctl enable --now veyquant-collector.service veyquant-certificate.timer veyquant-analysis.service
systemctl restart nginx
systemctl is-active veyquant-management veyquant-credentials veyquant-collector nginx
