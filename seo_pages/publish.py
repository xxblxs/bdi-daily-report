#!/usr/bin/env python3
"""
Publish generated report pages: upload to Aliyun OSS + ping search engines.

All credentials come from environment variables (store as GitHub Actions secrets):
  OSS_ENDPOINT, OSS_BUCKET, OSS_AK_ID, OSS_AK_SECRET   — Aliyun OSS
  BAIDU_PUSH_TOKEN                                       — 百度搜索资源平台 普通收录 token
  INDEXNOW_KEY                                           — Bing/IndexNow key (hosted as <key>.txt)

Usage:
  python publish.py <out_dir> --urls url1 url2 ...        # upload + ping these new URLs
Dry run (no creds) prints what it would do.
"""
import argparse
import os
import pathlib
import sys
import urllib.request

SITE = "https://www.navgreen.cn"


def upload_oss(out_dir: pathlib.Path):
    """Upload out_dir/reports/** + sitemap-reports.xml to OSS under the same keys."""
    try:
        import oss2  # pip install oss2
    except ImportError:
        print("  ! oss2 not installed — skipping OSS upload (pip install oss2)")
        return False
    ep, bucket_name = os.getenv("OSS_ENDPOINT"), os.getenv("OSS_BUCKET")
    ak, sk = os.getenv("OSS_AK_ID"), os.getenv("OSS_AK_SECRET")
    if not all([ep, bucket_name, ak, sk]):
        print("  ! OSS_* env not set — skipping upload (dry run)")
        return False
    bucket = oss2.Bucket(oss2.Auth(ak, sk), ep, bucket_name)
    n = 0
    for f in out_dir.rglob("*"):
        if not f.is_file():
            continue
        key = str(f.relative_to(out_dir))
        ctype = "text/html; charset=utf-8" if f.suffix == ".html" else (
            "application/xml" if f.suffix == ".xml" else "application/octet-stream")
        bucket.put_object_from_file(key, str(f), headers={"Content-Type": ctype})
        n += 1
    print(f"  ✓ OSS: uploaded {n} files to {bucket_name}")
    return True


def ping_baidu(urls: list[str]):
    token = os.getenv("BAIDU_PUSH_TOKEN")
    if not token:
        print("  ! BAIDU_PUSH_TOKEN not set — skipping 百度 push")
        return
    # `site` must match the verified property on 百度搜索资源平台 exactly,
    # which for navgreen is the full https form (verified: https://www.navgreen.cn).
    api = f"http://data.zz.baidu.com/urls?site={SITE}&token={token}"
    payload = "\n".join(urls).encode()
    try:
        req = urllib.request.Request(api, data=payload,
                                     headers={"Content-Type": "text/plain"})
        resp = urllib.request.urlopen(req, timeout=15).read().decode()
        print(f"  ✓ 百度 push: {resp}")
    except Exception as e:
        print(f"  ! 百度 push failed: {e}")


def ping_indexnow(urls: list[str]):
    key = os.getenv("INDEXNOW_KEY")
    if not key:
        print("  ! INDEXNOW_KEY not set — skipping IndexNow")
        return
    import json as _json
    body = _json.dumps({
        "host": "www.navgreen.cn", "key": key,
        "keyLocation": f"{SITE}/{key}.txt", "urlList": urls,
    }).encode()
    try:
        req = urllib.request.Request("https://api.indexnow.org/indexnow", data=body,
                                     headers={"Content-Type": "application/json"})
        code = urllib.request.urlopen(req, timeout=15).getcode()
        print(f"  ✓ IndexNow (Bing/Yandex): HTTP {code}")
    except Exception as e:
        print(f"  ! IndexNow failed: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--urls", nargs="*", default=[])
    args = ap.parse_args()
    out = pathlib.Path(args.out_dir)

    upload_oss(out)
    if args.urls:
        ping_baidu(args.urls)
        ping_indexnow(args.urls)
    else:
        print("  (no --urls given; upload only)")


if __name__ == "__main__":
    main()
