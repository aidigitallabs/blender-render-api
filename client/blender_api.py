#!/usr/bin/env python3
"""Client for blender-render-api (stdlib only).  env: BLENDER_API_URL, BLENDER_API_KEY
  blender_api.py health | gpu | projects | jobs
  blender_api.py push <dir> <project> [--replace] [--exclude pat ...]
  blender_api.py run <project> [--no-wait] -- <cmd...>          e.g. -- blender -b --factory-startup -P build_scene.py
  blender_api.py job <id> [--tail 40] | log <id> | kill <id>
  blender_api.py pull <project> <glob> <outdir>                e.g. pull home-alone-v3 'clip_frames/*.png' ./frames
"""
import fnmatch, io, json, os, sys, time, uuid, zipfile, urllib.request as u

URL = os.environ.get("BLENDER_API_URL", "http://127.0.0.1:8800").rstrip("/")
KEY = os.environ.get("BLENDER_API_KEY", "")
EXCL = ["__pycache__", "*.pyc", "*.blend1", "_jobs", "*.log", "*.mp4", "clip_frames*", "frames", "test", "refs", ".git"]


def req(method, path, data=None, ctype="application/json", raw=False):
    body = None if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())
    r = u.Request(URL + path, data=body, method=method, headers={"X-Api-Key": KEY, "Content-Type": ctype})
    with u.urlopen(r, timeout=600) as resp:
        return resp.read() if raw else json.loads(resp.read() or b"null")


def zipdir(d, excludes):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if not any(fnmatch.fnmatch(x, e) for e in excludes)]
            for f in files:
                if any(fnmatch.fnmatch(f, e) for e in excludes):
                    continue
                p = os.path.join(root, f)
                z.write(p, os.path.relpath(p, d).replace(os.sep, "/"))
    return buf.getvalue()


def push(d, name, replace, excludes):
    data = zipdir(d, excludes)
    b = uuid.uuid4().hex
    head = f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="p.zip"\r\nContent-Type: application/zip\r\n\r\n'
    body = head.encode() + data + f"\r\n--{b}--\r\n".encode()
    print(f"uploading {len(data)/1e6:.1f} MB ...")
    return req("POST", f"/projects/{name}?replace={int(replace)}", body, f"multipart/form-data; boundary={b}")


def wait(jid):
    seen = 0
    while True:
        j = req("GET", f"/jobs/{jid}?tail=200")
        for line in j["tail"][seen:]:
            print("  |", line)
        seen = len(j["tail"])
        if j["status"] not in ("queued", "running"):
            print(f"JOB {jid} {j['status']} rc={j.get('rc')} {(j.get('finished', 0) - j.get('started', 0)):.0f}s")
            return j
        time.sleep(5)


def main(a):
    if not a or not KEY:
        sys.exit(__doc__)
    c = a[0]
    if c in ("health", "gpu", "projects", "jobs"):
        print(json.dumps(req("GET", f"/{c}"), indent=1))
    elif c == "push":
        d, name = a[1], a[2]
        ex = EXCL + [a[i + 1] for i, x in enumerate(a) if x == "--exclude"]
        print(push(d, name, "--replace" in a, ex))
    elif c == "run":
        name = a[1]
        cmd = a[a.index("--") + 1:]
        j = req("POST", "/jobs", {"project": name, "cmd": cmd})
        print("queued", j["id"])
        if "--no-wait" not in a:
            wait(j["id"])
    elif c == "job":
        tail = a[a.index("--tail") + 1] if "--tail" in a else 40
        print(json.dumps(req("GET", f"/jobs/{a[1]}?tail={tail}"), indent=1))
    elif c == "log":
        print(req("GET", f"/jobs/{a[1]}/log", raw=True).decode("utf-8", "replace"))
    elif c == "kill":
        print(req("DELETE", f"/jobs/{a[1]}"))
    elif c == "pull":
        name, glob, out = a[1], a[2], a[3]
        data = req("GET", f"/projects/{name}/zip?glob={u.quote(glob)}", raw=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(out)
            print(f"pulled {len(z.namelist())} files ({len(data)/1e6:.1f} MB) -> {out}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
