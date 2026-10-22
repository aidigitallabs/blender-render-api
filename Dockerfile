# blender-render-api: headless Blender 5.2 LTS behind a small job API.
# Nothing runs as root. All project files live under /work (a volume). No GUI, no add-ons, no network use at runtime.
FROM ubuntu:24.04
ARG BLENDER_VER=5.2.2
ARG BLENDER_SHA256=84098912789dc450e95697c4184fb8a90acbe5111c2ba4aede3fecb57806a168
ENV DEBIAN_FRONTEND=noninteractive \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=all \
    PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils python3 python3-venv ffmpeg \
      libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 libegl1 libxkbcommon0 libsm6 libice6 \
    && rm -rf /var/lib/apt/lists/*
# Blender: official tarball from blender.org, sha256 pinned.
RUN curl -fsSL -o /tmp/blender.tar.xz "https://download.blender.org/release/Blender${BLENDER_VER%.*}/blender-${BLENDER_VER}-linux-x64.tar.xz" \
    && echo "${BLENDER_SHA256}  /tmp/blender.tar.xz" | sha256sum -c - \
    && mkdir -p /opt/blender && tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 \
    && rm /tmp/blender.tar.xz
# API server + the two libs the texture generator needs (numpy, pillow). Blender ships its own Python.
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir fastapi uvicorn python-multipart numpy pillow
COPY api/server.py /opt/api/server.py
RUN (userdel -r ubuntu 2>/dev/null || true) && useradd -m -u 1000 render && mkdir -p /work && chown render:render /work
USER render
WORKDIR /work
VOLUME /work
EXPOSE 8800
HEALTHCHECK --interval=60s --timeout=5s CMD curl -fs -H "X-Api-Key: $API_KEY" http://127.0.0.1:8800/health || exit 1
ENTRYPOINT ["/opt/venv/bin/python", "/opt/api/server.py"]
