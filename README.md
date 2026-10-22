# blender-render-api

Headless Blender 5.2 LTS in a container, with a ~250-line job API in front of it. Built so a render box can be
lent to someone without handing out a login: the owner runs one container and shares a URL + API key, nothing else.

## Run it (GPU box)
```
git clone https://github.com/aidigitallabs/blender-render-api && cd blender-render-api
echo "API_KEY=$(openssl rand -hex 24)" > .env
docker compose up -d --build
curl -H "X-Api-Key: $(cut -d= -f2 .env)" http://127.0.0.1:8800/health
```
This builds the image from the Dockerfile in this repo: Ubuntu 24.04, the official Blender tarball from blender.org (sha256 pinned),
ffmpeg, a small Python venv. A few minutes the first time. Nothing is pulled from a third-party registry.

Or without compose:
```
docker build -t blender-render-api .
docker run -d --name blender-api --restart unless-stopped --gpus all   -e API_KEY='change-me-to-something-long' -p 8800:8800 -v blender-work:/work blender-render-api
```
Needs the NVIDIA driver and the NVIDIA Container Toolkit. Without a GPU it still works, on CPU.
A prebuilt copy is also published at `ghcr.io/aidigitallabs/blender-render-api:latest` if you would rather pull than build.

## What is in the image
- `ubuntu:24.04`, the official Blender tarball from blender.org (sha256 pinned in the Dockerfile), ffmpeg, a Python venv with
  fastapi/uvicorn/numpy/pillow. No add-ons, no GUI, no outbound network use at runtime.
- Runs as user `render` (uid 1000). All project data lives in the `/work` volume.

## Security model, honestly
- The API key is the only lock. Keep the port on a VPN/Tailscale or behind a reverse proxy with TLS; do not put 8800 on the open internet.
- A job runs `blender`, `python`, `ffmpeg` or `bash` with cwd inside the project dir. That is arbitrary code **inside the container**.
  The container is the sandbox: no host mounts beyond the volume, non-root, `--gpus all` only.
- Kill switch: `docker stop blender-api`. Wipe everything: `docker volume rm blender-work`.

## API
See the docstring at the top of `api/server.py`. Typical flow with `client/blender_api.py`:
```
export BLENDER_API_URL=https://host BLENDER_API_KEY=...   # plus BLENDER_API_BASIC=user:pass if the proxy uses Basic Auth
python client/blender_api.py push ./home-alone-v3 home-alone-v3 --replace
python client/blender_api.py run home-alone-v3 -- python gen_textures.py
python client/blender_api.py run home-alone-v3 -- blender -b --factory-startup -P build_scene.py
python client/blender_api.py run home-alone-v3 -- blender -b home_alone_v3.blend -P anim_clip2.py -- 100 128 clip_frames_x 60 0 59 paint_cans
python client/blender_api.py pull home-alone-v3 'clip_frames_x/*.png' ./frames
```
One job runs at a time (one GPU). Jobs interrupted by an API restart are marked `lost`, not re-run.
