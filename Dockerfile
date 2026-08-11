# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
ARG CUDA_IMAGE=nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04@sha256:ac55d124da4882b497f732d8dfd9a702d5447a5f29d08d56da6f64f0a1eb34bc
FROM ${CUDA_IMAGE} AS runtime

ARG COMFYUI_REVISION=dec5d9450a5290bcf63430409ea41018e67f41c3
ARG RCLONE_RELEASE=1.75.0
ARG RCLONE_SHA256=aa2804e08f48250e71009c727124b6341cd0288465804a9a09d14663cabafbaa
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_INPUT=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl ffmpeg git python3.12 python3.12-venv unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSLo /tmp/rclone.zip \
       "https://github.com/rclone/rclone/releases/download/v${RCLONE_RELEASE}/rclone-v${RCLONE_RELEASE}-linux-amd64.zip" \
    && echo "${RCLONE_SHA256}  /tmp/rclone.zip" | sha256sum -c - \
    && unzip -q /tmp/rclone.zip -d /tmp/rclone \
    && install -m 0555 "/tmp/rclone/rclone-v${RCLONE_RELEASE}-linux-amd64/rclone" /usr/bin/rclone \
    && rm -rf /tmp/rclone /tmp/rclone.zip

COPY --from=ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b /uv /usr/local/bin/uv

RUN git clone https://github.com/Comfy-Org/ComfyUI.git /opt/ComfyUI \
    && git -C /opt/ComfyUI checkout --detach "${COMFYUI_REVISION}" \
    && test "$(git -C /opt/ComfyUI rev-parse HEAD)" = "${COMFYUI_REVISION}" \
    && rm -rf /opt/ComfyUI/.git

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY requirements ./requirements
RUN uv sync --frozen --no-dev --no-cache --no-install-project \
    && uv pip install --no-cache --require-hashes --torch-backend cu128 \
       -r requirements/comfyui.lock

COPY src ./src
RUN uv sync --frozen --no-dev --no-cache --inexact \
    && python -m tkr_cloud_video doctor --help \
    && rclone version \
    && ffprobe -version

COPY schemas ./schemas
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh \
    && groupadd --gid 65532 tkr-worker \
    && useradd --uid 65532 --gid 65532 --create-home \
       --home-dir /home/tkr-worker --shell /usr/sbin/nologin tkr-worker \
    && mkdir -p /cache /models /workspaces /outputs \
    && chown -R 65532:65532 /cache /models /workspaces /outputs /opt/ComfyUI/models

USER 65532:65532
EXPOSE 8188
ENTRYPOINT ["/entrypoint.sh"]
