# aux (GPU1, time-shared): Qwen3-VL enhancement + advisory QA, BiRefNet segmentation. Stateless.
FROM docker.io/library/python:3.12-slim-bookworm
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128
RUN pip install --no-cache-dir uv==0.12.1
WORKDIR /app
RUN uv pip install --system --no-cache torch==2.8.0 torchvision==0.23.0 --index-url ${TORCH_INDEX}
COPY services/prompt_service/requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt
COPY services/prompt_service/ /app/
COPY services/worker_common/lease.py /app/lease.py
COPY config/prompts/ /config/prompts/
RUN useradd --uid 1000 --create-home aux
USER aux
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONUNBUFFERED=1 MODELS_ROOT=/models PROMPTS_DIR=/config/prompts \
    HF_MODULES_CACHE=/tmp/hf_modules
EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
