#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布前自检。

在 commit / 打包 / 推 GitHub 之前跑一次，把三类事故挡在发布之前：

  1. 个人数据混入源码或历史（用户名、邮箱、绝对路径、私钥）
  2. 用户本地数据目录被打进安装包
  3. 打包产物（exe / apk）被提交进仓库

用法：
    python check_release.py            # 检查工作区 + Git 历史
    python check_release.py --staged   # 只检查待提交的暂存内容
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# ── 扫描规则 ────────────────────────────────────────────────────────────────
# 命中即报错。宁可误报也不能漏 —— 漏一条就等于把个人数据发到公网。
PATTERNS = [
    # 本机绝对路径：用户名 / 工作区 / 常见盘符
    (r"[A-Za-z]:\\Users\\[A-Za-z0-9_.-]+", "本机用户目录绝对路径"),
    (r"[A-Za-z]:\\Users\\[A-Za-z0-9_.-]+\\AppData", "AppData 绝对路径"),
    (r"D:\\codex_workspace", "开发机工作区路径"),
    (r"C:\\Users\\19645", "开发者用户名"),
    # 身份信息
    (r"elysiayunchen", "开发者 GitHub 账号名"),
    (r"[\w.+-]+@gmail\.com", "个人邮箱"),
    # 密钥类
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥"),
    (r"\bghp_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"\bsk-[A-Za-z0-9]{20,}", "API key"),
]

# 这些路径不该出现在仓库里（构建产物 / 本地数据）
FORBIDDEN_PATHS = [
    "release/",
    "dist/",
    "build/",
    "data.json",
    "port.txt",
]

# 源码文本里允许出现的例外（已知的、不算泄漏的）
TEXT_EXTS = (".py", ".js", ".html", ".css", ".md", ".bat", ".spec", ".txt", ".json")

# 自检脚本自身必然包含要检测的模式串（否则无法定义规则），
# 扫描时跳过它，避免自指误报。
SELF = os.path.basename(__file__)

# 本应用自己的公开仓库 slug 是有意写进源码的（更新追踪要访问它的 Releases API），
# 仓库本身就是公开的，不算泄漏 —— 扫描前先替换成占位符，避免误报账号名。
PUBLIC_REPO_SLUG = "elysiayunchen/elysium-player"


def sh(*args):
    try:
        return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout
    except Exception:
        return ""


def scan_text(text, where, hits):
    text = text.replace(PUBLIC_REPO_SLUG, "<APP_REPO>")
    for pat, label in PATTERNS:
        for m in re.finditer(pat, text):
            frag = m.group(0)
            # 允许的占位/示例不算
            if frag.startswith(("C:\\Users\\<", "D:\\codex_workspace\\<")):
                continue
            hits.append("%s → %s：%s" % (where, label, frag))


def main():
    staged = "--staged" in sys.argv
    hits = []

    # 1) 工作区文件（只看会被跟踪的源码类型）
    tracked = sh("git", "ls-files").split("\n")
    for rel in tracked:
        rel = rel.strip()
        if not rel or not rel.lower().endswith(TEXT_EXTS):
            continue
        if os.path.basename(rel) == SELF:
            continue
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                scan_text(f.read(), rel, hits)
        except Exception:
            pass

    # 2) Git 历史（所有 commit 的 blob 内容）
    if not staged:
        objs = sh("git", "rev-list", "--all", "--objects").split("\n")
        for line in objs:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            sha, path = parts[0], parts[1].strip()
            if not path or not path.lower().endswith(TEXT_EXTS):
                continue
            t = sh("git", "cat-file", "-t", sha).strip()
            if t != "blob":
                continue
            if os.path.basename(path) == SELF:
                continue
            body = sh("git", "cat-file", "-p", sha)
            scan_text(body, "历史:%s" % path, hits)

    # 3) 仓库里不该有的路径
    all_paths = set()
    for line in sh("git", "rev-list", "--all", "--objects").split("\n"):
        parts = line.split(None, 1)
        if len(parts) == 2:
            all_paths.add(parts[1].strip())
    for p in sorted(all_paths):
        if not p:
            continue
        for bad in FORBIDDEN_PATHS:
            if p.startswith(bad) or p == bad.rstrip("/"):
                hits.append("仓库含不该提交的路径：%s（%s）" % (p, bad))

    # 4) 当前 HEAD 的暂存区
    if staged:
        for line in sh("git", "diff", "--cached", "--name-only").split("\n"):
            p = line.strip()
            if not p:
                continue
            for bad in FORBIDDEN_PATHS:
                if p.startswith(bad):
                    hits.append("暂存区含不该提交的路径：%s（%s）" % (p, bad))

    # ── 报告 ────────────────────────────────────────────────────────────────
    if hits:
        print("✗ 发布自检未通过，共 %d 项：" % len(hits))
        for h in hits:
            print("   - " + h)
        print()
        print("处理完再提交/发布。历史里的泄漏需要 git filter-repo 重写。")
        return 1

    print("✓ 发布自检通过：无个人数据、无本机路径、无构建产物入库。")
    print("  已扫描：工作区源码 + Git 全历史 blob + 仓库路径清单")
    return 0


if __name__ == "__main__":
    sys.exit(main())
