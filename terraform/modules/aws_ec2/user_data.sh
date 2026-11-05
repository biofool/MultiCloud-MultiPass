#!/bin/bash
# User-data script for EC2 GPU inference instance.
# Runs as root on first boot. Installs Docker + NVIDIA Container Toolkit
# and starts the vLLM or Ollama inference server.
#
# Template variables (provided by Terraform templatefile):
#   inference_engine     - "vllm" or "ollama"
#   vllm_model           - HuggingFace model ID
#   vllm_port            - vLLM API port
#   vllm_tensor_parallel - tensor parallel size
#   ollama_model         - Ollama model tag
#   ollama_port          - Ollama API port
#   model_volume_size_gb - model EBS volume size (0 = no separate volume)

set -euo pipefail

exec > >(tee /var/log/user-data.log) 2>&1
echo "=== EC2 AI inference user-data started at $(date -u) ==="

# ---------------------------------------------------------------------------
# Mount model volume (if present)
# ---------------------------------------------------------------------------
if [ "${model_volume_size_gb}" -gt 0 ]; then
  echo "Mounting model volume /dev/sdf at /models ..."
  mkdir -p /models
  # Check if the volume is already formatted; format if not.
  if ! blkid /dev/sdf >/dev/null 2>&1; then
    mkfs.ext4 /dev/sdf
  fi
  mount /dev/sdf /models
  echo "/dev/sdf /models ext4 defaults,nofail 0 2" >> /etc/fstab
  chmod 755 /models
fi

# ---------------------------------------------------------------------------
# Install Docker + NVIDIA Container Toolkit
# ---------------------------------------------------------------------------
echo "Installing Docker ..."
apt-get update -y
apt-get install -y ca-certificates curl gnupg lsb-utils

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable docker
systemctl start docker

echo "Installing NVIDIA Container Toolkit ..."
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update -y
apt-get install -y nvidia-container-toolkit

nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

# Verify GPU is visible to Docker
echo "Verifying GPU access ..."
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi || {
  echo "WARNING: nvidia-smi failed in Docker — drivers may not be loaded yet."
  echo "The Ubuntu NVIDIA GPU AMI should have drivers pre-installed."
}

# ---------------------------------------------------------------------------
# Start inference server
# ---------------------------------------------------------------------------
if [ "${inference_engine}" = "vllm" ]; then
  echo "Starting vLLM inference server (model: ${vllm_model}) ..."

  # HF token — set via SSM or env if the model is gated.
  # $${...} is escaped so Terraform's templatefile does not interpolate it —
  # HF_TOKEN is a runtime bash variable, not a Terraform template variable.
  HF_TOKEN="$${HF_TOKEN:-}"

  docker run -d \
    --name vllm \
    --gpus all \
    --shm-size 4g \
    -p ${vllm_port}:8000 \
    -v /models:/root/.cache/huggingface \
    $${HF_TOKEN:+-e HF_TOKEN=$${HF_TOKEN}} \
    --restart unless-stopped \
    ghcr.io/vllm/vllm-openai:latest \
    --model "${vllm_model}" \
    --tensor-parallel-size ${vllm_tensor_parallel} \
    --host 0.0.0.0 \
    --port 8000

elif [ "${inference_engine}" = "ollama" ]; then
  echo "Starting Ollama inference server (model: ${ollama_model}) ..."

  docker run -d \
    --name ollama \
    --gpus all \
    -p ${ollama_port}:11434 \
    -v /models:/root/.ollama \
    --restart unless-stopped \
    ollama/ollama:latest

  # Pull the model after the server is ready. Poll the /api/tags endpoint
  # until Ollama responds (a fixed sleep is a race — the server may not be
  # ready in 10s while pulling the runtime image, or may be ready sooner).
  echo "Waiting for Ollama server to be ready ..."
  for i in $(seq 1 60); do
    if docker exec ollama curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
      echo "Ollama ready after $${i}s — pulling model ${ollama_model} ..."
      break
    fi
    sleep 1
  done
  docker exec ollama ollama pull "${ollama_model}"

else
  echo "ERROR: unknown inference_engine '${inference_engine}'" >&2
  exit 1
fi

echo "=== EC2 AI inference user-data completed at $(date -u) ==="
