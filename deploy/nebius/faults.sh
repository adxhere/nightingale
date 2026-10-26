#!/usr/bin/env bash
# Cause GPU problems on purpose on the server set up by setup.sh, for drills and the demo video.
#
#   bash deploy/nebius/faults.sh status       what is running, and the GPU right now
#   bash deploy/nebius/faults.sh bug          a REAL bug: a GPU job reads outside its memory; the driver raises a real Xid
#   bash deploy/nebius/faults.sh crash        a REAL crash: the training job's process is killed
#   bash deploy/nebius/faults.sh xid79        INJECTED with DCGM's test tool: "GPU has fallen off the bus"
#   bash deploy/nebius/faults.sh xid48        INJECTED: double-bit ECC error
#   bash deploy/nebius/faults.sh xid94        INJECTED: contained ECC error
#   bash deploy/nebius/faults.sh xid74        INJECTED: NVLink error
#   bash deploy/nebius/faults.sh train        start (or restart) the training job; it resumes from its checkpoint
#   bash deploy/nebius/faults.sh repaired     after a drill: uncordon the node, clear injected values, restart training
#
# Injected faults use NVIDIA's own DCGM error injection (dcgmi test --inject): the value DCGM reports
# is set, the hardware is untouched. Say so when you show them. "bug" and "crash" are the real thing.
set -euo pipefail
cd "$(dirname "$0")/../.."
export KUBECONFIG=${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}
DCGM_POD() { kubectl -n gpu-operator get pod -l app=nvidia-dcgm -o name | head -1; }
inject() {  # field id, value
  local pod; pod=$(DCGM_POD)
  [ -n "$pod" ] || { echo "No DCGM host engine pod (setup.sh runs the GPU Operator with dcgm.enabled=true)."; exit 1; }
  kubectl -n gpu-operator exec "$pod" -- dcgmi test --inject --gpuid "${GPU:-0}" -f "$1" -v "$2"
}

case "${1:-help}" in
  status)
    kubectl get nodes -o wide
    kubectl get pods -A -o wide | grep -E 'NAMESPACE|ml |research |nightingale ' || true
    nvidia-smi --query-gpu=index,name,temperature.gpu,power.draw,power.limit,clocks.sm,utilization.gpu --format=csv
    ;;
  bug)
    echo "Pausing the training job (its checkpoint stays) so the buggy job gets the GPU..."
    kubectl -n ml delete job llm-train --ignore-not-found --wait=true
    kubectl -n research delete job embeddings-bug --ignore-not-found --wait=true
    kubectl apply -f deploy/k8s/demo-buggy.yaml
    echo "In about 30 s the job reads far outside its memory. Watch Nightingale's Incidents page."
    ;;
  crash)
    pod=$(kubectl -n ml get pod -l app=llm-train --field-selector=status.phase=Running -o name | head -1)
    [ -n "$pod" ] || { echo "The training job isn't running. Start it: faults.sh train"; exit 1; }
    kubectl -n ml exec "$pod" -- sh -c 'for p in /proc/[0-9]*; do grep -qa "/app/train.py" "$p/cmdline" 2>/dev/null && [ "${p#/proc/}" != 1 ] && [ "${p#/proc/}" != "$$" ] && kill -9 "${p#/proc/}"; done' || true
    echo "Killed the training process in $pod. Kubernetes starts a new pod; it resumes from its checkpoint."
    ;;
  xid79) inject 230 79; echo "Injected Xid 79 (GPU has fallen off the bus) into DCGM for GPU ${GPU:-0}." ;;
  xid48) inject 230 48; echo "Injected Xid 48 (double-bit ECC error) into DCGM for GPU ${GPU:-0}." ;;
  xid94) inject 230 94; echo "Injected Xid 94 (contained ECC error) into DCGM for GPU ${GPU:-0}." ;;
  xid74) inject 230 74; echo "Injected Xid 74 (NVLink error) into DCGM for GPU ${GPU:-0}." ;;
  train)
    kubectl -n research delete job embeddings-bug --ignore-not-found --wait=true
    kubectl -n ml delete job llm-train --ignore-not-found --wait=true
    kubectl apply -f deploy/k8s/demo-training.yaml
    echo "Training started; it resumes from its last checkpoint. Logs: kubectl -n ml logs -f job/llm-train"
    ;;
  repaired)
    for n in $(kubectl get nodes -o name); do kubectl uncordon "${n#node/}"; done
    inject 230 0 >/dev/null 2>&1 || true
    "$0" train
    echo "Marked as repaired. In Nightingale, close the repair ticket (Incidents > Tickets) once the GPU looks healthy."
    ;;
  *)
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
