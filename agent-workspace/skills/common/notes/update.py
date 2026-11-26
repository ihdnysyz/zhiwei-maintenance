#!/usr/bin/env python3
"""项目自动更新触发器：欢迎语和 Stop hook 查询版本、启动后台进程。

实际更新委托 scripts/command/update.sh / agentdev update 共用的跨平台核心，不在项目副本中拉取代码或执行全局 setup。
--welcome 输出版本提示；--run [--remote N] 后台检查全局安装并刷新当前工作区。
update-ready 保留失败重试；锁与日志位于当前工作区 .agent/env/。旧 SessionStart 的无参数调用保持空操作。
"""
import datetime
import os
import subprocess
import sys
import time

sys.dont_write_bytecode = True  # 不在工作区副本中生成 __pycache__

from common import ENV, load_config, project_root, python_path, skill_dir, update_stamp_file
from _version import parse_version, format_version, fetch_remote_version

READY = ENV / "update-ready"
LOCK = ENV / "update.lock"
LOG = ENV / "update.log"
LOG_LIMIT = 200_000  # 字节，超过时只保留后半部分
NICE = 10


def log(msg):
    try:
        if LOG.exists() and LOG.stat().st_size > LOG_LIMIT:
            LOG.write_bytes(LOG.read_bytes()[-LOG_LIMIT // 2:])
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [{os.getpid()}] {msg}\n")
    except OSError:
        pass


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running():
    """已有更新进程在运行时返回 True。"""
    try:
        return alive(int(LOCK.read_text().strip()))
    except (OSError, ValueError):
        return False


def acquire():
    """取得互斥锁；已有存活的更新进程时返回 False。进程退出后遗留的锁视为失效。"""
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if running():
                return False
            LOCK.unlink(missing_ok=True)
            continue
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        return True
    return False


def release():
    try:
        if LOCK.read_text().strip() == str(os.getpid()):
            LOCK.unlink()
    except OSError:
        pass


def spawn(*args):
    """启动脱离调用方的后台进程：新会话、不继承终端和输出管道，调用方立即返回。"""
    ENV.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as err:
        subprocess.Popen([str(python_path(project_root())), os.path.abspath(__file__), *args],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                         close_fds=True, start_new_session=True)


def due(root, cfg):
    """时间戳缺失、损坏或晚于当前时间（时钟回拨）时都视为到期。"""
    try:
        last = float(update_stamp_file(root).read_text().strip())
    except (OSError, ValueError):
        return True
    return not 0 <= time.time() - last < float(cfg["auto_update_interval_hours"]) * 3600


def trigger(root, cfg, now=False, remote=None):
    """now 为 True 或检查到期时写入检查时间并启动后台进程；remote 为已取得的远端版本，后台进程不再请求。
    启动了后台进程时返回 True。任何异常都不影响调用方。"""
    try:
        if not cfg.get("auto_update", True) or running():
            return False
        if not (now or due(root, cfg)):
            return False
        stamp = update_stamp_file(root)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(f"{int(time.time())}\n")
        spawn("--run", *(["--remote", format_version(remote)] if remote is not None else []))
        return True
    except Exception as e:  # noqa: BLE001  hook 中不能因更新失败而报错
        log(f"触发更新失败：{e!r}")
        return False


def read_version(text):
    try:
        return parse_version(text, allow_legacy=True)
    except ValueError:
        return None


def local_version(skill):
    try:
        return read_version((skill / "publish/version").read_text(encoding="utf-8")) or (0, 0, 0)
    except OSError:
        return (0, 0, 0)


def fetch_remote(cfg):
    """共享主仓库版本源与旧地址迁移规则，不请求已废弃服务。"""
    try:
        return fetch_remote_version(cfg.get("update_url"))
    except RuntimeError as error:
        log(f"获取远端版本失败：{error}")
        return None


def check(root, cfg, remote=None):
    """项目 hook 只负责触发；实际更新由独立 update 入口的跨平台核心执行。"""
    skill = skill_dir()
    if not skill:
        log("找不到 AgentDevWorkSpace，请先 source setup.sh，再 agentdev init")
        return
    manager = skill / "scripts" / "agentdev.py"
    if not manager.is_file():
        log("旧版安装缺少全局更新入口，请先执行新版 setup，再 agentdev init")
        return
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    if cfg.get("update_url"):
        env["AGENTDEV_VERSION_URL"] = cfg["update_url"]
    if (skill / ".agentdev-app").is_file():
        env["AGENTDEV_HOME"] = str(skill.parent)
    with LOG.open("a", encoding="utf-8") as out:
        command = [str(python_path(root)), str(manager), "update", "--project", str(root)]
        if remote is not None:
            command += ["--remote", format_version(remote)]
        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=out, env=env, timeout=600)
    if result.returncode:
        READY.write_text((format_version(remote) if remote is not None else "retry") + "\n")
        log(f"更新失败（退出码 {result.returncode}），下次触发时重试")
    else:
        READY.unlink(missing_ok=True)


def setup(root):
    """重试当前工作区初始化；setup 命令本身现在只负责全局安装。"""
    skill = skill_dir()
    if not skill:
        return
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    if (skill / ".agentdev-app").is_file():
        env["AGENTDEV_HOME"] = str(skill.parent)
    with LOG.open("a", encoding="utf-8") as out:
        result = subprocess.run([str(python_path(root)), str(skill / "scripts/command/app.py"), "init", str(root)],
                                stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env, timeout=120)
    if result.returncode == 0:
        READY.unlink(missing_ok=True)


def installed_risk(skill):
    """Use the installed checker; ordinary source projects are not managed app state."""
    if not skill or not (skill / ".agentdev-app").is_file():
        return "", False
    sys.path.insert(0, str(skill / "scripts"))
    try:
        from lib.repository import installation_risk
        return installation_risk(skill.parent)
    except (ImportError, OSError):
        return "风险提示：安装目录缺少 Git 检查器，无法检查本地改动；请从 master 源码运行 setup.sh 重装。", False


def welcome(root):
    """欢迎语中的版本行：当场请求远端版本；本地落后或有待重试的 setup.sh 时立即启动后台更新。"""
    cfg = load_config(root)
    skill = skill_dir()
    warning, blocked = installed_risk(skill)
    local = local_version(skill) if skill else None
    remote = fetch_remote(cfg)
    if blocked:
        state = "安装目录存在风险，已暂停自动更新"
    elif running():
        state = "正在后台更新"
    elif local is None:
        state = "找不到 AgentDevWorkSpace，请重新执行 setup.sh"
    elif remote is None:
        state = "获取远端版本失败，后台重试" if trigger(root, cfg, now=True) else "获取远端版本失败"
    elif local < remote or READY.exists():
        if not cfg.get("auto_update", True):
            state = "有新版本，自动更新已关闭"
        else:
            state = "正在后台更新" if trigger(root, cfg, now=True, remote=remote) else "有新版本，启动更新失败"
    else:
        state = "已是最新"
    local_text = format_version(local) if local is not None else "未知"
    remote_text = format_version(remote) if remote is not None else "未知"
    if local is not None and remote is not None and local < remote:
        line = f"发现远端版本更新，由本地{local_text}版本更新到{remote_text}版本"
        if state != "正在后台更新":
            line += f"（{state}）"
    else:
        line = f"AgentDevWorkSpace 版本：本地 {local_text}，远端 {remote_text}（{state}）"
    print(line + ("\n" + warning if warning else ""))


def main():
    root = project_root()
    args = sys.argv[1:]
    if not args:  # 旧版本注册的 SessionStart hook：检查已改由欢迎语触发
        return 0
    if args == ["--welcome"]:
        welcome(root)
        return 0
    if args[:1] != ["--run"] or not (len(args) == 1 or (len(args) == 3 and args[1] == "--remote" and read_version(args[2]) is not None)):
        sys.exit(f"用法：{sys.argv[0]} --welcome | --run [--remote <版本号>]")
    remote = read_version(args[2]) if len(args) == 3 else None
    if hasattr(os, "nice"):
        os.nice(NICE)
    if not acquire():
        return 0
    try:
        if READY.exists():  # 上次更新后初始化失败，先补执行
            log("补执行上次失败的工作区初始化")
            setup(root)
        check(root, load_config(root), remote)
    except Exception as e:  # noqa: BLE001
        log(f"更新异常：{e!r}")
    finally:
        release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
