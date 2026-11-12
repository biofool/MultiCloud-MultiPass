#!/usr/bin/env bash
# Launch the vakt analysis box. Tries instance types in order until one has
# capacity, then waits for SSH. Log: ~/logs/vakt-analysis-<ts>.log
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
TYPES=(${VAKT_TYPES:-g6e.2xlarge g6.4xlarge g6e.12xlarge})
export AWS_PROFILE="${AWS_PROFILE:-ai-pentest-test}"

mkdir -p ~/logs
LOG=~/logs/vakt-analysis-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
echo "log: $LOG"

MYIP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
echo "operator ip: $MYIP"

tofu -chdir="$DIR" init -input=false

launched=""
AZS=(${VAKT_AZS:-us-east-1a us-east-1c us-east-1d us-east-1b})
for t in "${TYPES[@]}"; do
  for az in "${AZS[@]}"; do
    echo "=== trying $t in $az ==="
    if tofu -chdir="$DIR" apply -auto-approve -input=false \
         -var "instance_type=$t" -var "availability_zone=$az" \
         -var "operator_cidr=$MYIP/32"; then
      launched="$t ($az)"
      break 2
    fi
    echo "apply failed for $t in $az; trying next"
  done
done
[[ -n "$launched" ]] || { echo "no instance type launched"; exit 1; }

IP="$(tofu -chdir="$DIR" output -raw public_ip)"
echo "launched $launched at $IP; waiting for SSH"
for i in $(seq 1 40); do
  if ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 \
       -i ~/.ssh/id_ed25519 "ubuntu@$IP" \
       'hostname; nproc; free -g | sed -n 2p; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader'; then
    echo "SSH OK: ssh -i ~/.ssh/id_ed25519 ubuntu@$IP"
    exit 0
  fi
  sleep 15
done
echo "SSH not reachable after 10 min"
exit 1
