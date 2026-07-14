"""List all objects in the Backblaze vault with sizes, grouped by prefix."""
import os
from collections import defaultdict

import boto3


def load_env(path=".env"):
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v)


def client():
    load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))
    return boto3.client(
        "s3",
        endpoint_url=os.environ["DS_ENDPOINT"],
        region_name=os.environ["DS_REGION"],
        aws_access_key_id=os.environ["DS_KEY"],
        aws_secret_access_key=os.environ["DS_SECRET"],
    )


def main():
    s3 = client()
    bucket = os.environ["DS_BUCKET"]
    paginator = s3.get_paginator("list_objects_v2")
    objs = []
    for page in paginator.paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            objs.append((o["Key"], o["Size"]))
    print(f"total objects: {len(objs)}, total size: {sum(s for _, s in objs)/1e9:.2f} GB")
    groups = defaultdict(lambda: [0, 0])
    for k, s in objs:
        parts = k.split("/")
        prefix = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "(root)")
        groups[prefix][0] += 1
        groups[prefix][1] += s
    for p in sorted(groups):
        n, sz = groups[p]
        print(f"{p:60s} {n:6d} files {sz/1e6:10.1f} MB")
    with open(os.path.join(os.path.dirname(__file__), "..", "data", "_manifest.txt"), "w") as f:
        for k, s in sorted(objs):
            f.write(f"{s}\t{k}\n")
    print("manifest written to data/_manifest.txt")


if __name__ == "__main__":
    os.makedirs(os.path.join(os.path.dirname(__file__), "..", "data"), exist_ok=True)
    main()
