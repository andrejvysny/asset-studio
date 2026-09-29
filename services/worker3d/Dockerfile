# worker3d (GPU1, time-shared under Studio ownership): TRELLIS.2 image->3D sampling + GLB export. Stateless.
# Versions follow TRELLIS.2 setup.sh (torch 2.6.0 / CUDA 12.4); all git deps pinned.
# Default image ships NO NVIDIA non-commercial code (nvdiffrast/nvdiffrec): the clean exporter rasterises UV space in
# PyTorch and a stub satisfies o_voxel's import. RESEARCH_EXPORTER=1 adds real nvdiffrast for the opt-in
# research/evaluation exporter.
FROM nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04

ARG TRELLIS_REF=75fbf0183001ed9876c8dbb35de6b68552ee08bd
ARG CUMESH_REF=12289e1062f0603f2f0d0771b02e1395d247f26f
ARG FLEXGEMM_REF=6dd94a859c26ee8246888502eada3dd8ad85532e
ARG UTILS3D_REF=9a4eb15e4021b67b12c460c7057d642626897ec8
ARG NVDIFFRAST_TAG=v0.4.0
ARG RESEARCH_EXPORTER=0
# 8.9 = RTX 4090. Add archs for other GPUs, e.g. "8.6;8.9;9.0".
ARG TORCH_CUDA_ARCH_LIST=8.9
ARG MAX_JOBS=8

ENV DEBIAN_FRONTEND=noninteractive TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST} MAX_JOBS=${MAX_JOBS}
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.10 python3.10-dev python3-pip git ninja-build build-essential \
      libjpeg-dev libgl1 libglib2.0-0 libegl1 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.10 /usr/bin/python
RUN python -m pip install --no-cache-dir --upgrade pip==25.3 setuptools==80.9.0 wheel==0.45.1

RUN pip install --no-cache-dir torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124
COPY services/worker3d/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt \
    && pip install --no-cache-dir git+https://github.com/EasternJournalist/utils3d.git@${UTILS3D_REF}
RUN pip install --no-cache-dir flash-attn==2.7.3 --no-build-isolation

WORKDIR /tmp/ext
RUN git clone --recursive https://github.com/JeffreyXiang/CuMesh.git && git -C CuMesh checkout ${CUMESH_REF} \
    && git -C CuMesh submodule update --init --recursive \
    && pip install --no-cache-dir ./CuMesh --no-build-isolation
RUN git clone --recursive https://github.com/JeffreyXiang/FlexGEMM.git && git -C FlexGEMM checkout ${FLEXGEMM_REF} \
    && git -C FlexGEMM submodule update --init --recursive \
    && pip install --no-cache-dir ./FlexGEMM --no-build-isolation
RUN git clone https://github.com/microsoft/TRELLIS.2.git /opt/TRELLIS.2 && git -C /opt/TRELLIS.2 checkout ${TRELLIS_REF} \
    && git -C /opt/TRELLIS.2 submodule update --init --recursive \
    && pip install --no-cache-dir /opt/TRELLIS.2/o-voxel --no-build-isolation
COPY services/worker3d/stubs/ /tmp/stubs/
RUN SITE=$(python -c "import site; print(site.getsitepackages()[0])") \
    && if [ "${RESEARCH_EXPORTER}" = "1" ]; then \
         git clone -b ${NVDIFFRAST_TAG} https://github.com/NVlabs/nvdiffrast.git \
         && pip install --no-cache-dir ./nvdiffrast --no-build-isolation; \
       else cp -r /tmp/stubs/nvdiffrast "$SITE/"; fi \
    && pip uninstall -y nvdiffrec_render 2>/dev/null; rm -rf /tmp/ext /tmp/stubs

WORKDIR /app
COPY services/worker3d/*.py /app/
RUN useradd --uid 1000 --create-home worker3d
USER worker3d
ENV PYTHONPATH=/opt/TRELLIS.2 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONUNBUFFERED=1 MODELS_ROOT=/models/checkpoints \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True OPENCV_IO_ENABLE_OPENEXR=1 \
    TRITON_CACHE_DIR=/tmp/triton RESEARCH_EXPORTER=${RESEARCH_EXPORTER}
EXPOSE 8003
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"]
