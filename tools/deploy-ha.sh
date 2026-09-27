#!/bin/sh
# Copy the integration to Home Assistant (without the dev harness), then check the config.
# Restart afterwards: ssh -i ~/.ssh/claude_hassio root@192.168.3.27 ha core restart
set -e
HA=root@192.168.3.27
KEY=~/.ssh/claude_hassio
cd "$(dirname "$0")/.."
ssh -i $KEY $HA 'cd /config && tar czf /config/ecovacs_mower_backup_$(date +%Y%m%d_%H%M).tgz custom_components/ecovacs_mower'
tar czf - --exclude=__pycache__ --exclude=dev -C custom_components ecovacs_mower \
  | ssh -i $KEY $HA 'rm -rf /config/custom_components/ecovacs_mower && tar xzf - -C /config/custom_components && ha core check'
