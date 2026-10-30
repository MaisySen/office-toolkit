"""Keep the SQLite auth database across restarts of a stateless host.

restore: download the last encrypted snapshot (if any) to TOOLKIT_AUTH_DB.
loop:    every DOCTOOL_SNAPSHOT_SECONDS (default 60) take a consistent copy
         with the SQLite backup API, encrypt it (Fernet, DOCTOOL_STATE_KEY)
         and upload it to a private Hugging Face dataset repo when it
         changed (free private storage; only ciphertext leaves the host). Also on
         SIGTERM, so a normal restart loses nothing.

Env: HF_TOKEN, DOCTOOL_STATE_REPO (owner/name, private dataset),
DOCTOOL_STATE_KEY (Fernet key). Values are never logged.
"""
import hashlib
import os
import signal
import sqlite3
import sys
import tempfile
import time

from cryptography.fernet import Fernet

DB = os.environ.get("TOOLKIT_AUTH_DB", "/data/toolkit-auth.db")
REPO = os.environ.get("DOCTOOL_STATE_REPO", "")
NAME = "toolkit-auth.db.enc"
EVERY = int(os.environ.get("DOCTOOL_SNAPSHOT_SECONDS", "60"))
SQUASH_EVERY = 6 * 3600


def _api():
    from huggingface_hub import HfApi
    return HfApi(token=os.environ.get("HF_TOKEN"))


def _fernet():
    return Fernet(os.environ["DOCTOOL_STATE_KEY"].encode())


def restore():
    if not (REPO and os.environ.get("DOCTOOL_STATE_KEY")):
        print("persist: not configured, starting empty", flush=True)
        return
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import EntryNotFoundError, RepositoryNotFoundError
    try:
        path = hf_hub_download(REPO, NAME, repo_type="dataset",
                               token=os.environ.get("HF_TOKEN"),
                               cache_dir=tempfile.mkdtemp())
    except (EntryNotFoundError, RepositoryNotFoundError):
        print("persist: no snapshot yet, starting empty", flush=True)
        return
    data = _fernet().decrypt(open(path, "rb").read())
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    tmp = DB + ".restore"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, DB)
    print(f"persist: restored {len(data)} bytes", flush=True)


def _snapshot() -> bytes | None:
    if not os.path.exists(DB):
        return None
    src = sqlite3.connect(DB)
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    data = open(tmp, "rb").read()
    os.unlink(tmp)
    return data


def loop():
    if not (REPO and os.environ.get("DOCTOOL_STATE_KEY")):
        return
    api = _api()
    last = None
    last_squash = time.time()
    stop = {"now": False}

    def on_term(*_):
        stop["now"] = True
    signal.signal(signal.SIGTERM, on_term)

    while True:
        try:
            data = _snapshot()
            if data is not None:
                digest = hashlib.sha256(data).hexdigest()
                if digest != last:
                    blob = _fernet().encrypt(data)
                    api.upload_file(path_or_fileobj=blob, path_in_repo=NAME,
                                    repo_id=REPO, repo_type="dataset",
                                    commit_message="snapshot")
                    last = digest
            if time.time() - last_squash > SQUASH_EVERY:
                api.super_squash_history(repo_id=REPO, repo_type="dataset")
                last_squash = time.time()
        except Exception as e:  # keep serving; report the type only
            print(f"persist: snapshot failed ({type(e).__name__})", flush=True)
        if stop["now"]:
            return
        for _ in range(EVERY):
            if stop["now"]:
                break
            time.sleep(1)


if __name__ == "__main__":
    {"restore": restore, "loop": loop}[sys.argv[1]]()
