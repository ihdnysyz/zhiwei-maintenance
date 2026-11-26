"""工作区统一配置与个人目录规则，供初始化、Note 和 SDD 复用。"""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from _version import normalize_url

sys.dont_write_bytecode = True


def config_path(root):
    directory = root / "agent-workspace/config"
    current = directory / "config.json5"
    return current if current.exists() or not (directory / "notes.json5").exists() else directory / "notes.json5"


def load_config(root, defaults=None):
    import json5
    defaults = defaults or Path(__file__).parent / "notes/config.json5"
    cfg = json5.loads(defaults.read_text(encoding="utf-8"))
    path = config_path(root)
    user = json5.loads(path.read_text(encoding="utf-8") or "{}") if path.exists() else {}
    if not isinstance(user, dict):
        raise ValueError(f"配置必须是对象：{path}")
    user_scan = user.get("scan", {})
    if not isinstance(user_scan, dict) or not isinstance(user_scan.get("large_change", {}), dict):
        raise ValueError(f"scan / large_change 必须是对象：{path}")
    scan = {**cfg["scan"], **user_scan}
    scan["large_change"] = {**cfg["scan"]["large_change"], **user_scan.get("large_change", {})}
    archive = user.get("archive", {})
    if not isinstance(archive, dict):
        raise ValueError(f"archive 必须是对象：{path}")
    archive = {**cfg.get("archive", {"threshold": 50, "group_size": 100}), **archive}
    for key in ("threshold", "group_size"):
        if type(archive.get(key)) is not int or archive[key] <= 0:
            raise ValueError(f"archive.{key} 必须是正整数：{path}")
    cfg.update(user)
    cfg["archive"] = archive
    cfg["scan"] = scan
    if isinstance(cfg.get("update_url"), str):
        cfg["update_url"] = normalize_url(cfg["update_url"])
    return cfg


def git(root, *args):
    env = dict(os.environ)
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    result = subprocess.run(["git", "-C", str(root), *args], env=env, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def slug(name):
    name = re.sub(r"[A-Z]", lambda match: match.group().lower(), name)
    return re.sub(r"[\x00-\x20\x7f!-/:-@\[-`{-~]+", "-", name).strip("-")


def owner_name(root):
    top = git(root, "rev-parse", "--show-toplevel")
    if top and Path(top).resolve() == root.resolve():
        return git(root, "config", "user.name")
    return getpass.getuser()


def personal_name(root, cfg=None):
    cfg = load_config(root) if cfg is None else cfg
    for key in ("personal", "peronsal"):
        if key in cfg and not isinstance(cfg[key], str):
            raise ValueError(f"{key} 必须是字符串，留空时使用默认用户名")
    selected = cfg.get("personal") or cfg.get("peronsal") or owner_name(root)
    if not isinstance(selected, str) or not selected.strip():
        raise ValueError("请配置 personal；Git 项目也可配置 git user.name")
    if selected.strip().lower() in {".", "..", ".agent"} or any(char in selected for char in ("/", "\\", "\x00")):
        raise ValueError("personal 必须是单个安全目录名，不能包含路径")
    name = slug(selected)
    reserved = {"skills", "config", ".agent", "workers"}
    if not name or name in reserved:
        raise ValueError("personal 目录名不能为空或使用工作区保留名称")
    return name


def personal_dir(root, cfg=None):
    return root / "agent-workspace" / "workers" / personal_name(root, cfg)


def migrate_version_source(root):
    """仅替换实际生效的旧 URL 字面量，保留其他配置和注释。"""
    import json5
    path = config_path(root)
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    cfg = json5.loads(text)
    old = cfg.get("update_url")
    if not isinstance(old, str) or normalize_url(old) == old:
        return False
    target = normalize_url(old)
    pattern = re.compile(r'''(?P<prefix>(?:"update_url"|'update_url'|\bupdate_url)\s*:\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''')
    for match in pattern.finditer(text):
        candidate = text[:match.start()] + match["prefix"] + json.dumps(target) + text[match.end():]
        try:
            if json5.loads(candidate).get("update_url") == target:
                path.write_text(candidate, encoding="utf-8")
                return True
        except ValueError:
            continue
    raise ValueError(f"无法安全迁移版本地址，请手动设置 {path} 中的 update_url 为 {target}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--personal", action="store_true")
    parser.add_argument("--owner", action="store_true")
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    if args.migrate:
        migrate_version_source(args.root)
    cfg = load_config(args.root)
    name = personal_name(args.root, cfg)
    if args.personal:
        print(name)
    elif args.owner:
        print(owner_name(args.root) or name)
    else:
        print(json.dumps({"config": cfg, "personal": name, "owner": owner_name(args.root) or name}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"工作区配置错误：{error}", file=sys.stderr)
        sys.exit(1)
