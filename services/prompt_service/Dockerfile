# prompt-service (GPU1, time-shared): Qwen3-VL-8B enhance + QA.
FROM python:3.12-slim-bookworm
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128
RUN pip install --no-cache-dir uv==0.12.1
WORKDIR /app
RUN uv pip install --system --no-cache torch==2.8.0 torchvision==0.23.0 --index-url ${TORCH_INDEX}
COPY services/prompt_service/requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt
COPY services/prompt_service/ /app/
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONUNBUFFERED=1
EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
