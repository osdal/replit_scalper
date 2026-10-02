#!/usr/bin/env bash
# ============================================================
#  Подготовка свежего Ubuntu-инстанса в Oracle Cloud под торгового бота.
#  Запуск:  sudo bash deploy/oci/bootstrap-ubuntu.sh
#
#  Делает:
#    1. Обновляет пакеты, ставит Docker Engine + compose plugin.
#    2. Создаёт своп (важно для инстансов с 1 ГБ RAM).
#    3. Ограничивает рост docker-логов (иначе забьётся диск).
#    4. Создаёт каталоги проекта и ставит umask/права на env-файлы.
#    5. Включает unattended-upgrades.
#
#  Docker ставится из репозитория Ubuntu (docker.io + docker-compose-v2),
#  чтобы не зависеть от docker.com.
# ============================================================
set -euo pipefail

echo "=== [1/5] System update ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

echo "=== [2/5] Docker Engine + compose ==="
apt-get install -y --no-install-recommends \
  docker.io docker-compose-v2 ca-certificates curl git rsync jq unattended-upgrades
systemctl enable --now docker
systemctl enable docker

echo "=== [3/5] Swap (2G) ==="
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show

echo "=== [4/5] Docker log rotation ==="
if [ ! -f /etc/docker/daemon.json ]; then
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" }
}
JSON
  systemctl restart docker
fi

echo "=== [5/5] Project dirs ==="
mkdir -p /opt/trading-bot/{data,logs}
chmod 700 /opt/trading-bot
# Секреты (.env / .env.live) должны быть 600 — их тоже монтируют контейнеры.
[ -f /opt/trading-bot/.env ] && chmod 600 /opt/trading-bot/.env || true
[ -f /opt/trading-bot/.env.live ] && chmod 600 /opt/trading-bot/.env.live || true

echo
echo "=== Done ==="
docker --version
docker compose version
echo "Next: copy the repo, .env/.env.live, data/ and logs/, then run ./stack.sh"
