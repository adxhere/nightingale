#!/usr/bin/env bash
# Nightingale on a rented NVIDIA GPU server (Nebius AI Cloud, or any Ubuntu GPU machine).
#
# Installs, on this one machine:
#   1. the NVIDIA Container Toolkit (if the image doesn't have it),
#   2. Kubernetes (k3s, a small single-node Kubernetes),
#   3. NVIDIA's GPU Operator, which runs DCGM and dcgm-exporter (real GPU health telemetry),
#   4. a small real training job that saves checkpoints,
#   5. Nightingale, connected to all of it with FLEET_DCGM=auto, as a service on port 8000.
#
# Run from the Nightingale folder:   bash deploy/nebius/setup.sh
# Then, on your laptop:              ssh -L 8000:localhost:8000 <user>@<server ip>
#                                    and open http://localhost:8000
# Safe to run again: every step checks what is already there.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$(pwd)"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
step() { printf '\n\033[1;33m[%s] %s\033[0m\n' "$1" "$2"; }
die() { printf '\n\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

step 0/6 "Checking the GPU"
command -v nvidia-smi >/dev/null || die "nvidia-smi isn't here. Create the VM from an image with NVIDIA drivers (on Nebius: an 'Ubuntu ... for NVIDIA GPUs (CUDA)' image)."
nvidia-smi -L || die "The NVIDIA driver doesn't see a GPU."

step 1/6 "NVIDIA Container Toolkit"
if ! command -v nvidia-ctk >/dev/null; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq nvidia-container-toolkit
fi
nvidia-ctk --version | head -1

step 2/6 "Kubernetes (k3s)"
if ! command -v k3s >/dev/null; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--write-kubeconfig-mode 644" sh -
else
  sudo systemctl restart k3s   # picks up the NVIDIA runtime if the toolkit was just installed
fi
for _ in $(seq 60); do kubectl get nodes 2>/dev/null | grep -q " Ready" && break; sleep 3; done
kubectl get nodes
grep -q nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml \
  || die "k3s didn't register the NVIDIA runtime. Check that nvidia-container-runtime is on the PATH, then run this again."
kubectl apply -f - <<'YAML'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata: {name: nvidia}
handler: nvidia
YAML

step 3/6 "NVIDIA GPU Operator with DCGM (a few minutes the first time)"
if ! command -v helm >/dev/null; then
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi
kubectl create namespace gpu-operator --dry-run=client -o yaml | kubectl apply -f -
kubectl -n gpu-operator create configmap nightingale-dcgm-metrics \
  --from-file=dcgm-metrics.csv="$ROOT/deploy/k8s/dcgm-metrics.csv" --dry-run=client -o yaml | kubectl apply -f -
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia >/dev/null 2>&1 || true
helm repo update nvidia >/dev/null
# Drivers and the toolkit are already on the host. dcgm.enabled runs a standalone DCGM host engine,
# which also lets deploy/nebius/faults.sh use NVIDIA's own DCGM error injection for drills.
helm upgrade --install gpu-operator nvidia/gpu-operator -n gpu-operator --wait --timeout 15m \
  --set driver.enabled=false --set toolkit.enabled=false \
  --set dcgm.enabled=true \
  --set dcgmExporter.config.name=nightingale-dcgm-metrics \
  --set 'dcgmExporter.env[0].name=DCGM_EXPORTER_INTERVAL' --set-string 'dcgmExporter.env[0].value=5000'
for _ in $(seq 90); do
  kubectl -n gpu-operator get pods -l app=nvidia-dcgm-exporter 2>/dev/null | grep -q Running && break; sleep 5
done
kubectl -n gpu-operator get pods
kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}' | grep -q '[1-9]' \
  || die "Kubernetes doesn't see the GPU yet. Look at: kubectl -n gpu-operator get pods"

step 4/6 "A real training job (it saves checkpoints every 500 steps)"
kubectl apply -f "$ROOT/deploy/k8s/demo-training.yaml"
kubectl apply -f "$ROOT/deploy/k8s/nightingale-rbac.yaml"

step 5/6 "Nightingale"
sudo apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -q --upgrade pip
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt" -r "$ROOT/requirements-fleet.txt"
if [ ! -f "$ROOT/.env" ]; then
  key="${NEBIUS_API_KEY:-}"
  if [ -z "$key" ]; then
    read -r -s -p "Nebius Token Factory API key (Enter for demo mode, no key): " key; echo
  fi
  umask 077
  {
    [ -n "$key" ] && echo "NEBIUS_API_KEY=$key"
    echo "FLEET_DCGM=auto"
    echo "FLEET_KUBE=on"
    echo "FLEET_KUBECONFIG=/etc/rancher/k3s/k3s.yaml"
    echo "LOCAL_GPU=false"
    echo "NUM_GPUS=256"
  } > "$ROOT/.env"
fi
sudo tee /etc/systemd/system/nightingale.service >/dev/null <<UNIT
[Unit]
Description=Nightingale GPU agent
After=network-online.target k3s.service

[Service]
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
User=$(id -un)

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now nightingale
sudo systemctl restart nightingale
for _ in $(seq 30); do curl -fs http://127.0.0.1:8000/api/health >/dev/null && break; sleep 2; done
curl -fs http://127.0.0.1:8000/api/fleet | python3 -c 'import json,sys; d=json.load(sys.stdin); print("Real servers:", d.get("servers"), "GPUs:", d.get("gpus"), "Kubernetes:", d.get("kube"), d.get("error") or "")'

step 6/6 "Done"
ip=$(curl -fs -m 3 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
cat <<MSG

Nightingale is running on this server and watching it through DCGM and Kubernetes.
On your laptop, open a tunnel and the dashboard:

    ssh -L 8000:localhost:8000 $(id -un)@$ip
    http://localhost:8000      (Fleet > Real servers)

Cause problems on purpose with:   bash deploy/nebius/faults.sh help
Logs:                              journalctl -u nightingale -f
When you're done, DELETE THE VM in the Nebius console so billing stops.
MSG
