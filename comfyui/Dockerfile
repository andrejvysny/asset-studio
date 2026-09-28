# ComfyUI (GPU0): UI/API + Qwen-Image-2512 + Line A bridge nodes.
FROM python:3.12-slim-bookworm

ARG COMFYUI_REF=v0.37.4
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.12.1

RUN git clone --depth 1 --branch ${COMFYUI_REF} https://github.com/comfyanonymous/ComfyUI.git /opt/ComfyUI
WORKDIR /opt/ComfyUI

RUN uv pip install --system --no-cache torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0 --index-url ${TORCH_INDEX}
# constraints.txt pins the transitive set; regenerate with `make lock-comfyui`.
COPY comfyui/constraints.txt /tmp/constraints.txt
RUN uv pip install --system --no-cache -r requirements.txt -c /tmp/constraints.txt

COPY comfyui/custom_nodes/line_a/requirements.txt /tmp/line_a_req.txt
RUN uv pip install --system --no-cache -r /tmp/line_a_req.txt -c /tmp/constraints.txt

COPY comfyui/extra_model_paths.yaml /opt/ComfyUI/extra_model_paths.yaml
COPY comfyui/custom_nodes/line_a /opt/ComfyUI/custom_nodes/line_a

ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 DO_NOT_TRACK=1 PYTHONUNBUFFERED=1
EXPOSE 8188
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", \
     "--disable-auto-launch", "--output-directory", "/output/_comfy", \
     "--user-directory", "/comfy-user", \
     "--temp-directory", "/tmp/comfy", "--input-directory", "/output/_comfy_input"]
