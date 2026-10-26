# Nightingale on a real NVIDIA GPU server (Nebius AI Cloud)

This turns one rented GPU machine into a small, real GPU cluster that Nightingale watches and repairs:
Kubernetes, NVIDIA's GPU Operator with DCGM (the health telemetry real data centers use), a real training
job that saves checkpoints, and Nightingale connected to all of it. About 20 minutes from start to
dashboard, and a few dollars of GPU time.

Everything Nightingale shows for this server is real: DCGM readings, Kubernetes nodes, pods and events.
When you approve a step, it really cordons the node, evicts the job's pods or runs `nvidia-smi`, and
each plan is first rehearsed with Kubernetes' server-side dry run, which changes nothing.

## 1. Create the VM

In the [Nebius AI Cloud console](https://console.nebius.com): **Compute → Virtual machines → Create
resource → Virtual machine**.

| Step | Choose |
|---|---|
| Compute | Platform **With GPUs**, an **NVIDIA L40S** platform (the cheapest, about $1.55/hour), preset with **1 GPU** |
| Storage | Boot disk image **Ubuntu 24.04 LTS for NVIDIA® GPUs (CUDA® 13)** (driver, Container Toolkit and DCGM preinstalled), size **100 GiB** (the PyTorch image is large) |
| Network | Public IP address **Auto (dynamic)** |
| Configuration | **Username and SSH key**: a username (not `root` or `admin`) and your public SSH key |

If the console says you have no GPU quota, request it from Nebius support first.

## 2. Set it up

```bash
ssh <username>@<public IP>
git clone https://github.com/adxhere/nightingale.git && cd nightingale
bash deploy/nebius/setup.sh
```

It asks for your Nebius Token Factory API key (typed, not shown, saved only in `.env` on this VM with
owner-only permissions). Press Enter to run in demo mode instead.

When it finishes, open a tunnel from your laptop and the dashboard:

```bash
ssh -L 8000:localhost:8000 <username>@<public IP>
```

Then http://localhost:8000 → **Fleet → Real servers**. The VM shows up as a real server in rack R-01,
running the `llm-train` job, next to the simulated data center.

## 3. Cause problems on purpose

```bash
bash deploy/nebius/faults.sh status     # what runs, and the GPU right now
bash deploy/nebius/faults.sh bug        # REAL: a job reads outside its memory; the driver raises a real Xid
bash deploy/nebius/faults.sh crash      # REAL: the training process is killed
bash deploy/nebius/faults.sh xid79      # INJECTED with DCGM's test tool: "GPU has fallen off the bus"
bash deploy/nebius/faults.sh xid48      # INJECTED: double-bit ECC error
bash deploy/nebius/faults.sh repaired   # after a drill: uncordon, clear injected values, restart training
```

`bug` and `crash` are genuine failures. The `xid…` drills use NVIDIA's own DCGM error injection
(`dcgmi test --inject`): DCGM reports the error exactly as it would a real one, but the hardware is
untouched. Say which is which when you record.

What to expect for `xid79`: Nemotron Nano flags the Xid within a few seconds, Nemotron Ultra investigates
with read-only tools and proposes: cordon the node, move `llm-train`, open a repair ticket. Kubernetes
accepts all three in a dry run; you approve; the node is cordoned and the job's pod evicted. With one
server there is nowhere else to run, so the new pod waits (Pending) until the node is repaired. Then run
`faults.sh repaired`, close the ticket in Nightingale, and the job resumes from its last checkpoint
(`kubectl -n ml logs -f job/llm-train` shows "resumed from checkpoint at step …").

To see a job actually move to another server, add a second GPU VM to the cluster as a k3s agent:
on the first VM run `sudo cat /var/lib/rancher/k3s/server/node-token`, then on the second
`curl -sfL https://get.k3s.io | K3S_URL=https://<first VM's private IP>:6443 K3S_TOKEN=<token> sh -`.
The GPU Operator sets the new node up by itself.

## 4. Stop paying

When you're done, **delete the VM** in the console (Compute → Virtual machines → … → Delete). A stopped
VM can still bill for its disk and IP.

## Troubleshooting

- `kubectl -n gpu-operator get pods`: everything should be Running or Completed. If `nvidia-dcgm` keeps
  restarting, the image's own DCGM service may be holding the GPU: `sudo systemctl stop nvidia-dcgm` and
  `kubectl -n gpu-operator delete pod -l app=nvidia-dcgm`.
- `journalctl -u nightingale -f` shows Nightingale's log. `curl -s localhost:8000/api/fleet` shows what
  it reads.
- The first start of `llm-train` pulls a large PyTorch image; give it a few minutes
  (`kubectl -n ml get pods -w`).
