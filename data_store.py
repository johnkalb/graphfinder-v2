"""Nightly data files live in DigitalOcean Spaces, not in git (2026-11-26).

Every nightly rebuild used to commit ~200 MB of regenerated .gz files, and the
repository history grew to ~11.5 GB -- DigitalOcean builds started dying while
cloning it. Now the files are uploaded to the private Space `graph-history`
under data/<sha256>/<name>, and only webapp/data/manifest.json (name -> sha256,
size) is committed. The Docker build runs `python data_store.py fetch`, which
downloads whatever the manifest lists, checks every hash and fails the build
on any mismatch. Rolling back a graph = reverting one manifest commit.

Credentials: HISTORY_SPACE = "<access key id>:<secret key>", from the
environment (a BUILD_TIME secret on the DO app, passed to the Dockerfile as a
build arg) or, on the laptop, from graphfinder-clean/.secrets.local.env.

  python data_store.py publish webapp/data/graph_scored.json.gz ...   upload + update manifest
  python data_store.py fetch                                         download what's missing
  python data_store.py verify                                        hash-check local copies
"""
import hashlib
import json
import os
import sys

ENDPOINT = "https://sfo3.digitaloceanspaces.com"
BUCKET = "graph-history"
ROOT = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(ROOT, "webapp", "data", "manifest.json")
LOCAL_SECRETS = r"C:\Users\johnk\graphfinder-clean\.secrets.local.env"


def _credentials():
    v = os.environ.get("HISTORY_SPACE")
    if not v and os.path.exists(LOCAL_SECRETS):
        with open(LOCAL_SECRETS, encoding="utf-8") as f:
            for line in f:
                if line.startswith("HISTORY_SPACE="):
                    v = line.split("=", 1)[1].strip().strip('"')
    if not v or ":" not in v:
        sys.exit("HISTORY_SPACE is missing or not '<access key id>:<secret key>'")
    key_id, secret = v.split(":", 1)
    return key_id.strip(), secret.strip()


def _client():
    import warnings
    import boto3
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")  # laptop TLS interception
    key_id, secret = _credentials()
    return boto3.client("s3", endpoint_url=ENDPOINT, region_name="sfo3",
                        aws_access_key_id=key_id, aws_secret_access_key=secret)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest():
    if not os.path.exists(MANIFEST):
        return {"files": {}}
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def _key(rel, digest):
    return f"data/{digest}/{os.path.basename(rel)}"


def publish(paths):
    """Upload each file (skipped if that exact content is already there) and
    record it in the manifest. Paths are relative to the repo root."""
    s3 = _client()
    m = load_manifest()
    for p in paths:
        rel = os.path.relpath(os.path.abspath(p), ROOT).replace("\\", "/")
        full = os.path.join(ROOT, rel)
        digest = sha256(full)
        key = _key(rel, digest)
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
            print(f"  {rel}: already stored")
        except s3.exceptions.ClientError:
            s3.upload_file(full, BUCKET, key)
            print(f"  {rel}: uploaded ({os.path.getsize(full) / 1e6:.1f} MB)")
        m["files"][rel] = {"sha256": digest, "size": os.path.getsize(full)}
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(m, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, MANIFEST)


def _ok(rel, entry):
    full = os.path.join(ROOT, rel)
    return os.path.exists(full) and os.path.getsize(full) == entry["size"] and sha256(full) == entry["sha256"]


def fetch():
    m = load_manifest()
    todo = [(rel, e) for rel, e in sorted(m["files"].items()) if not _ok(rel, e)]
    if not todo:
        print(f"data_store: all {len(m['files'])} files present and verified")
        return
    s3 = _client()
    for rel, e in todo:
        full = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        s3.download_file(BUCKET, _key(rel, e["sha256"]), full + ".part")
        os.replace(full + ".part", full)
        if not _ok(rel, e):
            sys.exit(f"data_store: {rel} does not match its manifest hash")
        print(f"  {rel}: fetched ({e['size'] / 1e6:.1f} MB)")
    print(f"data_store: fetched {len(todo)}, all {len(m['files'])} verified")


def verify():
    bad = [rel for rel, e in load_manifest()["files"].items() if not _ok(rel, e)]
    for rel in bad:
        print(f"  MISMATCH or missing: {rel}")
    return not bad


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "publish" and len(sys.argv) > 2:
        publish(sys.argv[2:])
    elif cmd == "fetch":
        fetch()
    elif cmd == "verify":
        sys.exit(0 if verify() else 1)
    else:
        sys.exit(__doc__)
