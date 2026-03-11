#!/usr/bin/env python3
"""
从 Vivo 应用商店查询 Teams 国内版最新版本并下载 APK。

用法:
    # 仅检查版本
    python3 check_and_download.py --check

    # 检查并下载 (如果版本比指定的新)
    python3 check_and_download.py --download --min-version-code 2025624560

    # 输出 JSON 格式
    python3 check_and_download.py --check --json

环境变量:
    VIVO_APP_ID  — 覆盖默认的 Teams App ID (默认: 2368941)

输出到 stdout (JSON 格式 with --json):
    {"version_code": 2025624560, "version_name": "1416/1.0.0.2025224506",
     "download_url": "...", "apk_path": "...", "is_new": true}
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request

# Vivo 应用商店 Teams 应用 ID
DEFAULT_VIVO_APP_ID = "2368941"
API_BASE = "https://h5-api.appstore.vivo.com.cn"
USER_AGENT = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36"


def fetch_latest_version(app_id):
    """从 Vivo API 获取最新版本信息"""
    url = f"{API_BASE}/detail/{app_id}?frompage=messageh5&app_version=2100"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if not data.get("id"):
        raise RuntimeError(f"Vivo API 未返回有效数据: {data}")

    return {
        "app_id": str(data["id"]),
        "package_name": data.get("package_name", "com.microsoft.teams"),
        "version_name": data["version_name"],
        "version_code": int(data["version_code"]),
        "download_url": data["download_url"],
        "size_kb": data.get("size", 0),
        "upload_time": data.get("upload_time", ""),
        "title": data.get("title_zh", ""),
    }


def download_apk(download_url, output_dir, version_code):
    """使用 curl 或 wget 下载 APK"""
    filename = f"teams-china-{version_code}.apk"
    filepath = os.path.join(output_dir, filename)

    if os.path.exists(filepath):
        print(f"APK 已存在: {filepath}", file=sys.stderr)
        return filepath

    tmp_filepath = filepath + ".downloading"
    print(f"下载: {download_url}", file=sys.stderr)

    if shutil.which("axel"):
        subprocess.run([
            "axel", "-n", "8",
            "-a",  # 显示进度条
            "-o", tmp_filepath,
            download_url,
        ], check=True)
    elif shutil.which("curl"):
        subprocess.run([
            "curl", "-L",
            "--progress-bar",
            "--retry", "3",
            "--retry-delay", "5",
            "-o", tmp_filepath,
            download_url,
        ], check=True)
    elif shutil.which("wget"):
        subprocess.run([
            "wget",
            "--progress=bar:force",
            "--tries=3",
            "-O", tmp_filepath,
            download_url,
        ], check=True)
    else:
        # 回退到 Python urllib
        print("  (curl/wget 不可用，使用 Python 内置下载)", file=sys.stderr)
        req = urllib.request.Request(download_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=600) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(tmp_filepath, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 // total
                        print(f"\r  {downloaded // 1024 // 1024}MB / {total // 1024 // 1024}MB ({pct}%)",
                              end="", file=sys.stderr)
            print(file=sys.stderr)

    # 校验文件存在且非空
    if not os.path.exists(tmp_filepath) or os.path.getsize(tmp_filepath) == 0:
        raise RuntimeError("下载失败: 文件为空或不存在")

    os.rename(tmp_filepath, filepath)

    size_mb = os.path.getsize(filepath) / 1024 / 1024
    print(f"下载完成: {filepath} ({size_mb:.1f} MB)", file=sys.stderr)
    return filepath


def main():
    parser = argparse.ArgumentParser(description="Teams 国内版版本检查与下载工具")
    parser.add_argument("--check", action="store_true", help="仅检查最新版本")
    parser.add_argument("--download", action="store_true", help="下载 APK")
    parser.add_argument("--min-version-code", type=int, default=0,
                        help="最小版本号，仅当远程版本大于此值时下载")
    parser.add_argument("--output-dir", default=".", help="下载目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    parser.add_argument("--app-id", default=None, help="Vivo 应用 ID")

    args = parser.parse_args()

    if not args.check and not args.download:
        args.check = True

    app_id = args.app_id or os.environ.get("VIVO_APP_ID", DEFAULT_VIVO_APP_ID)

    info = fetch_latest_version(app_id)
    is_new = info["version_code"] > args.min_version_code

    result = {
        "version_code": info["version_code"],
        "version_name": info["version_name"],
        "package_name": info["package_name"],
        "upload_time": info["upload_time"],
        "size_kb": info["size_kb"],
        "is_new": is_new,
        "apk_path": None,
    }

    if not args.json:
        print(f"Teams 国内版最新版本:", file=sys.stderr)
        print(f"  名称:     {info['title']}", file=sys.stderr)
        print(f"  版本名:   {info['version_name']}", file=sys.stderr)
        print(f"  版本号:   {info['version_code']}", file=sys.stderr)
        print(f"  更新时间: {info['upload_time']}", file=sys.stderr)
        print(f"  大小:     {int(info['size_kb']) / 1024:.1f} MB", file=sys.stderr)
        if args.min_version_code:
            print(f"  是否更新: {'是' if is_new else '否'} (当前: {args.min_version_code})", file=sys.stderr)

    if args.download and is_new:
        apk_path = download_apk(info["download_url"], args.output_dir, info["version_code"])
        result["apk_path"] = apk_path
    elif args.download and not is_new:
        print("当前已是最新版本，跳过下载", file=sys.stderr)

    if args.json:
        print(json.dumps(result))
    else:
        # 为 GitHub Actions 输出设置变量
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"version_code={result['version_code']}\n")
                f.write(f"version_name={result['version_name']}\n")
                f.write(f"is_new={'true' if is_new else 'false'}\n")
                if result["apk_path"]:
                    f.write(f"apk_path={result['apk_path']}\n")


if __name__ == "__main__":
    main()
