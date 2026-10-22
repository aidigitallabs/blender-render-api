#!/usr/bin/env python3
"""blender-render-api: a tiny job runner in front of headless Blender.

Endpoints (all need header  X-Api-Key: <API_KEY>):
  GET    /health                          blender version, gpu line, queue length
  GET    /gpu                             nvidia-smi one-liner (null if no GPU)
  GET    /projects                        list project dirs under /work
  POST   /projects/{name}?replace=0|1     upload a .zip (multipart field "file"), extracted to /work/{name}
  DELETE /projects/{name}
  GET    /projects/{name}/files?glob=**/*.png     list files (path, size)
  GET    /projects/{name}/file/{path}     download one file
  GET    /projects/{name}/zip?glob=...    download matching files as one zip
  POST   /jobs      {"project": n, "cmd": ["blender", "-b", ...], "env": {}}   -> queued job (one at a time)
  GET    /jobs ; GET /jobs/{id}?tail=40 ; GET /jobs/{id}/log ; DELETE /jobs/{id} (kill)
cmd[0] must be one of: blender, python, ffmpeg, bash. It runs with cwd = /work/{project}.
The container is the sandbox and the API key is the lock: a job can run any code *inside* the container.
"""
import hmac, json, os, re, shutil, signal, subprocess, sys, tempfile, threading, time, uuid, zipfile
from collections import deque
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.background import BackgroundTask
import uvicorn

WORK = Path(os.environ.get("WORK_DIR", "/work")).resolve()
API_KEY = os.environ.get("API_KEY", "")
if len(API_KEY) < 16:
    sys.exit("API_KEY env var (>= 16 chars) is required")
ALLOWED = {"blender": "/opt/blender/blender", "python": "/opt/venv/bin/python", "ffmpeg": "/usr/bin/ffmpeg", "bash": "/bin/bash"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STATE = WORK / "_jobs.json"
JOBS: dict[str, dict] = {}
QUEUE: deque[str] = deque()
PROCS: dict[str, subprocess.Popen] = {}
LOCK = threading.Lock()
BLENDER_VERSION = "unknown"


def auth(x_api_key: str = Header(default="")):
    if not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "bad api key")


app = FastAPI(title="blender-render-api", dependencies=[Depends(auth)])


def project_dir(name: str, must_exist=True) -> Path:
    if not NAME_RE.match(name) or name.startswith("_"):
        raise HTTPException(400, "bad project name")
    p = WORK / name
    if must_exist and not p.is_dir():
        raise HTTPException(404, "no such project")
    return p


def safe_rel(base: Path, rel: str) -> Path:
    p = (base / rel).resolve()
    if p != base and base not in p.parents:
        raise HTTPException(400, "path escapes project")
    return p


def save_jobs():
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(JOBS, indent=1))
    tmp.replace(STATE)


def gpu_line():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def worker():
    while True:
        with LOCK:
            jid = QUEUE.popleft() if QUEUE else None
        if not jid:
            time.sleep(0.5)
            continue
        j = JOBS[jid]
        proj = WORK / j["project"]
        logp = Path(j["log"])
        logp.parent.mkdir(exist_ok=True)
        argv = [ALLOWED[j["cmd"][0]], *j["cmd"][1:]]
        j.update(status="running", started=time.time())
        save_jobs()
        with open(logp, "ab") as lf:
            lf.write((" ".join(argv) + "\n").encode())
            try:
                p = subprocess.Popen(argv, cwd=proj, stdout=lf, stderr=subprocess.STDOUT,
                                     env={**os.environ, **j.get("env", {})}, start_new_session=True)
                PROCS[jid] = p
                rc = p.wait()
            except Exception as ex:
                lf.write(f"SPAWN ERROR {ex}\n".encode())
                rc = -1
            PROCS.pop(jid, None)
        j.update(status="killed" if j.get("kill") else ("done" if rc == 0 else "failed"), rc=rc, finished=time.time())
        save_jobs()


@app.get("/health")
def health():
    return {"ok": True, "blender": BLENDER_VERSION, "gpu": gpu_line(), "queued": len(QUEUE),
            "running": [i for i, j in JOBS.items() if j["status"] == "running"]}


@app.get("/gpu")
def gpu():
    return {"gpu": gpu_line()}


@app.get("/projects")
def projects():
    return sorted(p.name for p in WORK.iterdir() if p.is_dir() and not p.name.startswith("_"))


@app.post("/projects/{name}")
def upload(name: str, file: UploadFile = File(...), replace: bool = False):
    dest = project_dir(name, must_exist=False)
    if replace and dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=WORK, suffix=".zip", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
    n = 0
    try:
        with zipfile.ZipFile(tmp.name) as z:
            for m in z.infolist():
                target = safe_rel(dest, m.filename)
                if m.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                n += 1
    except zipfile.BadZipFile:
        raise HTTPException(400, "not a zip")
    finally:
        os.remove(tmp.name)
    return {"project": name, "files": n}


@app.delete("/projects/{name}")
def delete_project(name: str):
    shutil.rmtree(project_dir(name))
    return {"deleted": name}


@app.get("/projects/{name}/files")
def list_files(name: str, glob: str = "**/*"):
    base = project_dir(name)
    out = []
    for p in sorted(base.glob(glob)):
        if p.is_file():
            out.append({"path": p.relative_to(base).as_posix(), "size": p.stat().st_size})
    return out


@app.get("/projects/{name}/file/{path:path}")
def get_file(name: str, path: str):
    p = safe_rel(project_dir(name), path)
    if not p.is_file():
        raise HTTPException(404, "no such file")
    return FileResponse(p, filename=p.name)


@app.get("/projects/{name}/zip")
def get_zip(name: str, glob: str = "**/*.png"):
    base = project_dir(name)
    tmp = tempfile.NamedTemporaryFile(dir=WORK, suffix=".zip", delete=False)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for p in sorted(base.glob(glob)):
            if p.is_file():
                z.write(p, p.relative_to(base).as_posix())
    tmp.close()
    return FileResponse(tmp.name, filename=f"{name}.zip", background=BackgroundTask(os.remove, tmp.name))


@app.post("/jobs")
def submit(body: dict):
    name = body.get("project", "")
    cmd = body.get("cmd")
    env = body.get("env") or {}
    proj = project_dir(name)
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        raise HTTPException(400, "cmd must be a non-empty list of strings")
    if cmd[0] not in ALLOWED:
        raise HTTPException(400, f"cmd[0] must be one of {sorted(ALLOWED)}")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise HTTPException(400, "env must map str to str")
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    JOBS[jid] = {"id": jid, "project": name, "cmd": cmd, "env": env, "status": "queued", "created": time.time(),
                 "log": str(proj / "_jobs" / f"{jid}.log")}
    with LOCK:
        QUEUE.append(jid)
    save_jobs()
    return JOBS[jid]


@app.get("/jobs")
def jobs():
    return [{k: v for k, v in j.items() if k != "env"} for j in sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)[:100]]


@app.get("/jobs/{jid}")
def job(jid: str, tail: int = 40):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "no such job")
    lines = []
    if os.path.exists(j["log"]):
        with open(j["log"], "rb") as f:
            lines = f.read()[-20000:].decode("utf-8", "replace").splitlines()[-tail:]
    return {**j, "tail": lines}


@app.get("/jobs/{jid}/log", response_class=PlainTextResponse)
def job_log(jid: str):
    j = JOBS.get(jid)
    if not j or not os.path.exists(j["log"]):
        raise HTTPException(404, "no such job/log")
    return Path(j["log"]).read_text("utf-8", "replace")


@app.delete("/jobs/{jid}")
def kill(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "no such job")
    j["kill"] = True
    with LOCK:
        if jid in QUEUE:
            QUEUE.remove(jid)
            j["status"] = "killed"
    p = PROCS.get(jid)
    if p:
        os.killpg(p.pid, signal.SIGTERM)
    save_jobs()
    return {"id": jid, "status": j["status"]}


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        BLENDER_VERSION = subprocess.run([ALLOWED["blender"], "--version"], capture_output=True, text=True, timeout=30).stdout.splitlines()[0]
    except Exception as ex:
        BLENDER_VERSION = f"unavailable ({ex})"
    if STATE.exists():
        JOBS.update(json.loads(STATE.read_text()))
        for j in JOBS.values():
            if j["status"] in ("queued", "running"):
                j["status"] = "lost"
    threading.Thread(target=worker, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8800")), log_level="info")
