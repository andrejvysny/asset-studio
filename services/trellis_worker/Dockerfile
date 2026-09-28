# trellis-worker (GPU1, time-shared): BiRefNet cutout + TRELLIS.2 + GLB export.
# Versions follow TRELLIS.2 setup.sh (torch 2.6.0 / CUDA 12.4); all git deps pinned.
FROM nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04

ARG TRELLIS_REF=75fbf0183001ed9876c8dbb35de6b68552ee08bd
ARG CUMESH_REF=12289e1062f0603f2f0d0771b02e1395d247f26f
ARG FLEXGEMM_REF=6dd94a859c26ee8246888502eada3dd8ad85532e
ARG NVDIFFREC_REF=b296927cc7fd01c2ac1087c8065c4d7248f72da4
ARG UTILS3D_REF=9a4eb15e4021b67b12c460c7057d642626897ec8
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
COPY services/trellis_worker/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt \
    && pip install --no-cache-dir git+https://github.com/EasternJournalist/utils3d.git@${UTILS3D_REF}
RUN pip install --no-cache-dir flash-attn==2.7.3 --no-build-isolation

WORKDIR /tmp/ext
RUN git clone -b v0.4.0 https://github.com/NVlabs/nvdiffrast.git \
    && pip install --no-cache-dir ./nvdiffrast --no-build-isolation
RUN git clone -b renderutils https://github.com/JeffreyXiang/nvdiffrec.git && git -C nvdiffrec checkout ${NVDIFFREC_REF} \
    && pip install --no-cache-dir ./nvdiffrec --no-build-isolation
RUN git clone --recursive https://github.com/JeffreyXiang/CuMesh.git && git -C CuMesh checkout ${CUMESH_REF} \
    && git -C CuMesh submodule update --init --recursive \
    && pip install --no-cache-dir ./CuMesh --no-build-isolation
RUN git clone --recursive https://github.com/JeffreyXiang/FlexGEMM.git && git -C FlexGEMM checkout ${FLEXGEMM_REF} \
    && git -C FlexGEMM submodule update --init --recursive \
    && pip install --no-cache-dir ./FlexGEMM --no-build-isolation
RUN git clone https://github.com/microsoft/TRELLIS.2.git /opt/TRELLIS.2 && git -C /opt/TRELLIS.2 checkout ${TRELLIS_REF} \
    && git -C /opt/TRELLIS.2 submodule update --init --recursive \
    && pip install --no-cache-dir /opt/TRELLIS.2/o-voxel --no-build-isolation
RUN rm -rf /tmp/ext

WORKDIR /app
COPY services/trellis_worker/ /app/
ENV PYTHONPATH=/opt/TRELLIS.2 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONUNBUFFERED=1 \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True OPENCV_IO_ENABLE_OPENEXR=1
EXPOSE 8002
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
