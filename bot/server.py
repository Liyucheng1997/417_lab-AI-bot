"""我的服务器：用 SSH 连上自己的服务器，随时看状态，跑几条简单命令，或者一键打开完整的 SSH 终端。

- 连接走 paramiko（纯 Python，打包进 exe 不用额外装东西），密钥或密码都行。
  密码存在 Windows 凭据管理器里（和随手记的博客密码一样），不写进文件。
- 可以从 ~/.ssh/config 一键导入已经配好的服务器（HostName / User / Port / IdentityFile 都会带过来）。
- 状态：一条 shell 脚本读 /proc，算出 CPU、内存、磁盘、网速、负载、运行时间、最忙的进程、Docker 容器、失败的服务。
  只有面板的「服务器」页开着时才每几秒刷新一次；连接闲置 10 分钟自动断开。
- 服务器指纹第一次连接时记下（data/known_hosts，也会读 ~/.ssh/known_hosts），以后变了就拒绝连接，防中间人。
- 「终端」按钮用 Windows 自带的 OpenSSH（ssh.exe）在 Windows Terminal 里打开一个真正的 SSH 会话。

Bot 自己操作服务器的工具在 tools/server.py。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import DATA_DIR

STATE_PATH = DATA_DIR / "servers.json"
KNOWN_HOSTS = DATA_DIR / "known_hosts"
SSH_DIR = Path.home() / ".ssh"
CRED_PREFIX = "MyAIBot/server/"
IDLE_CLOSE = 600      # 连接闲置多久自动断开（秒）
REFRESH_MIN = 2.5     # 两次状态刷新的最短间隔（秒）
RETRY_GAP = 15        # 连不上时多久重试一次（秒）
MAX_OUTPUT = 200_000  # 命令输出最多保留多少字符

# 一次读完所有状态。top 采样 0.5 秒（顺便得到这段时间里最忙的进程），前后各读一次 /proc/stat、/proc/net/dev 算 CPU 和网速
STATUS_SCRIPT = r"""
export LC_ALL=C
echo @host; hostname 2>/dev/null
echo @os; ( . /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" ) || uname -sr
echo @arch; uname -m
echo @load; cat /proc/loadavg
echo @ncpu; nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo
echo @t1; cat /proc/uptime
echo @cpu1; head -1 /proc/stat
echo @net1; tail -n +3 /proc/net/dev
echo @top
if command -v top >/dev/null 2>&1; then
  COLUMNS=200 top -b -n 2 -d 0.5 2>/dev/null | awk '/^top -/{n++} n==2' | sed -n '/^ *PID/,$p' | head -8
else sleep 0.5; fi
echo @t2; cat /proc/uptime
echo @cpu2; head -1 /proc/stat
echo @net2; tail -n +3 /proc/net/dev
echo @mem; grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo
echo @disk; df -P -k 2>/dev/null | tail -n +2
echo @users; who 2>/dev/null | wc -l
echo @failed; systemctl --failed --no-legend --plain 2>/dev/null | head -8
echo @docker; command -v docker >/dev/null 2>&1 && timeout 4 docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}' 2>/dev/null | head -20
echo @end
"""

# 常用命令：添加服务器时默认带上，主人可以在卡片里改
DEFAULT_COMMANDS = [
    {"name": "磁盘", "cmd": "df -h -x tmpfs -x devtmpfs -x squashfs -x overlay"},
    {"name": "内存", "cmd": "free -h"},
    {"name": "最忙的进程", "cmd": "ps -eo pid,user,pcpu,pmem,etime,comm --sort=-pcpu | head -15"},
    {"name": "Docker", "cmd": "docker ps -a --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'"},
    {"name": "端口", "cmd": "ss -tlnp 2>/dev/null | head -30"},
    {"name": "最近日志", "cmd": "journalctl -n 40 --no-pager -p warning 2>/dev/null || tail -n 40 /var/log/syslog"},
    {"name": "登录记录", "cmd": "last -n 10 -a 2>/dev/null"},
]

# 不会出现在列表里的 ssh config 主机（代码托管站）
SKIP_HOSTS = re.compile(r"(^|\.)(github\.com|gitlab\.com|bitbucket\.org|gitee\.com|ssh\.dev\.azure\.com)$", re.I)
SKIP_FS = {"tmpfs", "devtmpfs", "udev", "overlay", "none", "efivarfs", "shm", "squashfs"}
SKIP_MOUNT = re.compile(r"^/(snap|run|dev|sys|proc|boot/efi|var/lib/docker)(/|$)")


# ------------------------------------------------------------------ 保存的服务器列表
_lock = threading.RLock()


def load() -> list[dict]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")).get("servers", [])
    except Exception:
        return []


def _save(servers: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps({"servers": servers}, ensure_ascii=False, indent=2), encoding="utf-8")


def get(sid_or_name: str) -> dict | None:
    """按 id、名字或地址找服务器（Bot 的工具用名字找）。"""
    key = str(sid_or_name or "").strip().lower()
    servers = load()
    if not key:
        return servers[0] if len(servers) == 1 else None
    for s in servers:
        if key in (s["id"].lower(), s["name"].lower(), s["host"].lower(), (s.get("alias") or "").lower()):
            return s
    return None


def _password(sid: str) -> str:
    import win32cred
    try:
        return win32cred.CredRead(CRED_PREFIX + sid, win32cred.CRED_TYPE_GENERIC)["CredentialBlob"].decode("utf-16-le")
    except Exception:
        return ""


def _set_password(sid: str, pw: str) -> None:
    import win32cred
    if pw:
        win32cred.CredWrite({"Type": win32cred.CRED_TYPE_GENERIC, "TargetName": CRED_PREFIX + sid,
                             "UserName": "ssh", "CredentialBlob": pw,
                             "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE}, 0)
    else:
        try:
            win32cred.CredDelete(CRED_PREFIX + sid, win32cred.CRED_TYPE_GENERIC, 0)
        except Exception:
            pass


def public(s: dict) -> dict:
    """给网页看的（不含密码）。"""
    out = {k: s.get(k) for k in ("id", "name", "host", "port", "user", "auth", "key", "alias")}
    out["commands"] = s.get("commands") or []
    out["has_password"] = s.get("auth") == "password" and bool(_password(s["id"]))
    return out


def _clean(form: dict) -> dict:
    host = str(form.get("host") or "").strip()
    user = str(form.get("user") or "").strip()
    port = str(form.get("port") or "").strip()
    # 允许直接填 user@host:port
    m = re.match(r"^(?:([^@\s]+)@)?([^:\s]+)(?::(\d+))?$", host)
    if not m:
        raise ValueError("地址格式不对，填 IP 或域名，例如 192.168.1.10 或 root@example.com:22")
    user = user or (m.group(1) or "")
    host = m.group(2)
    port = int(port or m.group(3) or 22)
    if not user:
        raise ValueError("请填用户名（比如 root、ubuntu）")
    if not 0 < port < 65536:
        raise ValueError("端口要在 1–65535 之间")
    auth = "password" if form.get("auth") == "password" else "key"
    key = str(form.get("key") or "").strip()
    if auth == "key" and key and not Path(os.path.expanduser(key)).is_file():
        raise ValueError(f"找不到密钥文件：{key}")
    return {"name": str(form.get("name") or "").strip() or host, "host": host, "port": port, "user": user,
            "auth": auth, "key": key if auth == "key" else "", "alias": str(form.get("alias") or "").strip()}


def test_and_save(form: dict) -> dict:
    """先试连一下，连得上才保存。form 里带 id 就是修改。"""
    try:
        s = _clean(form)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    sid = str(form.get("id") or "") or uuid.uuid4().hex[:8]
    old = next((x for x in load() if x["id"] == sid), None)
    pw = str(form.get("password") or "")
    if s["auth"] == "password" and not pw and old:
        pw = _password(sid)  # 改别的字段时没重填密码，沿用原来的
    if s["auth"] == "password" and not pw:
        return {"ok": False, "error": "请填密码"}
    s["id"] = sid
    s["commands"] = (old or {}).get("commands") or [dict(c) for c in DEFAULT_COMMANDS]
    try:
        conn = Conn(s, pw)
        conn.connect()
        conn.close()
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}
    with _lock:
        servers = [x for x in load() if x["id"] != sid]
        if old:
            servers.insert(next(i for i, x in enumerate(load()) if x["id"] == sid), s)
        else:
            servers.append(s)
        _save(servers)
        _set_password(sid, pw if s["auth"] == "password" else "")
    drop(sid)  # 旧连接按新设置重连
    return {"ok": True, "id": sid}


def remove(sid: str) -> None:
    with _lock:
        _save([x for x in load() if x["id"] != sid])
    _set_password(sid, "")
    drop(sid)


def set_commands(sid: str, commands: list[dict]) -> list[dict]:
    cleaned = [{"name": str(c.get("name") or "").strip()[:20], "cmd": str(c.get("cmd") or "").strip()}
               for c in commands if str(c.get("cmd") or "").strip()]
    with _lock:
        servers = load()
        for s in servers:
            if s["id"] == sid:
                s["commands"] = [{"name": c["name"] or c["cmd"][:20], "cmd": c["cmd"]} for c in cleaned]
        _save(servers)
    return cleaned


def move(sid: str, delta: int) -> None:
    with _lock:
        servers = load()
        i = next((i for i, s in enumerate(servers) if s["id"] == sid), None)
        if i is None:
            return
        j = max(0, min(len(servers) - 1, i + delta))
        servers.insert(j, servers.pop(i))
        _save(servers)


# ------------------------------------------------------------------ ~/.ssh/config 和密钥
def ssh_config_hosts() -> list[dict]:
    """~/.ssh/config 里配好的服务器（去掉通配符和 GitHub 之类），用来一键导入。"""
    path = SSH_DIR / "config"
    if not path.exists():
        return []
    import paramiko
    try:
        cfg = paramiko.SSHConfig.from_path(str(path))
    except Exception:
        return []
    have = {(s.get("alias") or "").lower() for s in load()} | {s["host"].lower() for s in load()}
    out = []
    for alias in sorted(cfg.get_hostnames()):
        if any(c in alias for c in "*?!") or alias.lower() in have:
            continue
        o = cfg.lookup(alias)
        host = o.get("hostname", alias)
        if SKIP_HOSTS.search(host) or host.lower() in have:
            continue
        keys = o.get("identityfile") or []
        out.append({"alias": alias, "host": host, "user": o.get("user", ""), "port": int(o.get("port", 22)),
                    "key": _tilde(keys[0]) if keys else ""})
    return out


def _tilde(path: str) -> str:
    """把家目录下的密钥路径写成 ~/.ssh/id_x（和「选择密钥」下拉框里的写法一致）。"""
    p = Path(os.path.expanduser(path))
    try:
        return "~/" + p.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(p)


def key_files() -> list[str]:
    """~/.ssh 里的私钥（有同名 .pub 的那些），给「选择密钥」下拉框用。"""
    if not SSH_DIR.is_dir():
        return []
    out = []
    for p in sorted(SSH_DIR.iterdir()):
        if p.is_file() and not p.suffix and (p.with_name(p.name + ".pub")).exists():
            out.append("~/.ssh/" + p.name)
    return out


# ------------------------------------------------------------------ 连接
class _RememberNewHost:
    """第一次连的服务器：记下指纹（像 ssh 的 accept-new）。指纹变了 paramiko 会直接拒绝。"""

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        DATA_DIR.mkdir(exist_ok=True)
        with _lock:
            with open(KNOWN_HOSTS, "a", encoding="utf-8") as f:
                f.write(f"{hostname} {key.get_name()} {key.get_base64()}\n")


def friendly_error(e: Exception) -> str:
    import socket

    import paramiko
    msg = str(e) or e.__class__.__name__
    if isinstance(e, paramiko.BadHostKeyException):
        return "服务器指纹和上次记下的不一样（重装过系统？或者有人在中间拦截）。确认没问题的话，删掉这台服务器重新添加"
    if isinstance(e, paramiko.AuthenticationException):
        return "登录失败：用户名、密码或密钥不对（密钥登录要先把公钥放进服务器的 ~/.ssh/authorized_keys）"
    if isinstance(e, paramiko.PasswordRequiredException):
        return "这个密钥设了密码短语，暂时不支持，换一把没有密码短语的密钥或用密码登录"
    if isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in msg:
        return "连接超时：服务器关机了、地址不对，或者防火墙没开 SSH 端口"
    if isinstance(e, socket.gaierror):
        return "找不到这个地址（域名解析失败）"
    if isinstance(e, ConnectionRefusedError) or "refused" in msg.lower() or "10061" in msg:
        return "服务器拒绝连接：SSH 服务没开，或者端口不对"
    if isinstance(e, paramiko.SSHException) and "Error reading SSH protocol banner" in msg:
        return "对方不像是 SSH 服务（端口不对？）"
    if isinstance(e, OSError) and ("10051" in msg or "10065" in msg or "unreachable" in msg.lower()):
        return "网络不通（不在同一个局域网？）"
    return msg


class Conn:
    def __init__(self, s: dict, password: str | None = None):
        self.s = s
        self.password = password if password is not None else (_password(s["id"]) if s.get("auth") == "password" else "")
        self.client = None
        self.lock = threading.Lock()
        self.used = time.time()

    def connect(self):
        import paramiko
        c = paramiko.SSHClient()
        try:
            if (SSH_DIR / "known_hosts").exists():
                c.load_system_host_keys(str(SSH_DIR / "known_hosts"))
            if KNOWN_HOSTS.exists():
                c.load_host_keys(str(KNOWN_HOSTS))
        except Exception:
            pass
        c.set_missing_host_key_policy(_RememberNewHost())
        kw = dict(hostname=self.s["host"], port=int(self.s.get("port") or 22), username=self.s["user"],
                  timeout=8, banner_timeout=10, auth_timeout=10)
        if self.s.get("auth") == "password":
            kw.update(password=self.password, look_for_keys=False, allow_agent=False)
        elif self.s.get("key"):
            kw.update(key_filename=os.path.expanduser(self.s["key"]), look_for_keys=False, allow_agent=True)
        else:
            kw.update(look_for_keys=True, allow_agent=True)
        c.connect(**kw)
        c.get_transport().set_keepalive(30)
        self.client = c

    def alive(self) -> bool:
        t = self.client.get_transport() if self.client else None
        return bool(t and t.is_active())

    def close(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None

    def run(self, cmd: str, timeout: float = 30, stdin: str | None = None) -> tuple[int, str]:
        """执行一条命令，返回 (退出码, stdout+stderr)。超时返回已有的输出，退出码 -1。"""
        self.used = time.time()
        with self.lock:
            if not self.alive():
                self.close()
                self.connect()
            tr = self.client.get_transport()
        ch = tr.open_session(timeout=10)
        ch.set_combine_stderr(True)
        ch.exec_command(cmd)
        if stdin is not None:
            ch.sendall(stdin.encode())
            ch.shutdown_write()
        buf = bytearray()
        end = time.time() + timeout
        timed_out = False
        while True:
            if ch.recv_ready():
                buf += ch.recv(65536)
                if len(buf) > MAX_OUTPUT * 4:
                    del buf[: len(buf) - MAX_OUTPUT * 4]
                continue
            if ch.exit_status_ready() and not ch.recv_ready():
                break
            if time.time() > end:
                timed_out = True
                break
            time.sleep(0.03)
        while ch.recv_ready():
            buf += ch.recv(65536)
        code = -1 if timed_out else ch.recv_exit_status()
        ch.close()
        self.used = time.time()
        out = buf.decode("utf-8", "replace")[-MAX_OUTPUT:]
        if timed_out:
            out += f"\n[{int(timeout)} 秒还没结束，已停止等待。长时间运行的命令请用「终端」]"
        return code, out


# ------------------------------------------------------------------ 每台服务器的连接 + 最近状态
CONNS: dict[str, Conn] = {}
STATS: dict[str, dict] = {}   # id → {state: ok/connecting/error, stats, error, at}
_refreshing: set[str] = set()


def conn(sid: str) -> Conn:
    with _lock:
        c = CONNS.get(sid)
        if c is None:
            s = next((x for x in load() if x["id"] == sid), None)
            if not s:
                raise KeyError("没有这台服务器")
            c = CONNS[sid] = Conn(s)
        return c


def drop(sid: str) -> None:
    with _lock:
        c = CONNS.pop(sid, None)
        STATS.pop(sid, None)
    if c:
        c.close()


def run(sid: str, cmd: str, timeout: float = 30) -> dict:
    """面板 / Bot 执行命令。sudo 开头且是密码登录时，自动用保存的密码。"""
    cmd = cmd.strip()
    if not cmd:
        return {"ok": False, "error": "命令是空的"}
    started = time.time()
    try:
        c = conn(sid)
        stdin = None
        if c.password and re.match(r"^sudo\s", cmd) and not re.match(r"^sudo\s+(-\S*S)", cmd):
            cmd = "sudo -S -p '' " + cmd[5:].lstrip()
            stdin = c.password + "\n"
        code, out = c.run(cmd, timeout, stdin)
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}
    if "sudo: a terminal is required" in out or "sudo: a password is required" in out:
        out += "\n[这条命令要 sudo 密码。用密码登录的服务器会自动填；密钥登录的请用「终端」，或在服务器上给这个用户配免密 sudo]"
    return {"ok": True, "code": code, "output": out, "ms": int((time.time() - started) * 1000)}


def refresh(sid: str, emit=None) -> None:
    """后台刷新一台服务器的状态（已经在刷就跳过）。"""
    with _lock:
        if sid in _refreshing:
            return
        st = STATS.get(sid) or {}
        gap = RETRY_GAP if st.get("state") == "error" else REFRESH_MIN  # 连不上的别每 3 秒就去撞一次
        if st.get("at") and time.time() - st["at"] < gap:
            return
        _refreshing.add(sid)
        if not st:
            STATS[sid] = {"state": "connecting"}

    def work():
        try:
            c = conn(sid)
            code, out = c.run(STATUS_SCRIPT, timeout=15)
            stats = parse_stats(out)
            if not stats.get("uptime") and code != 0:
                raise RuntimeError(out.strip()[-300:] or f"状态脚本退出码 {code}")
            STATS[sid] = {"state": "ok", "stats": stats, "at": time.time()}
        except Exception as e:
            prev = STATS.get(sid) or {}
            STATS[sid] = {"state": "error", "error": friendly_error(e), "at": time.time(),
                          "stats": prev.get("stats"), "stale": True}
            with _lock:
                c = CONNS.pop(sid, None)
            if c:
                c.close()
        finally:
            _refreshing.discard(sid)
            if emit:
                emit({"type": "server", "event": "stats", "id": sid})
    threading.Thread(target=work, daemon=True, name=f"server-{sid}").start()


def close_idle() -> None:
    now = time.time()
    with _lock:
        idle = [k for k, c in CONNS.items() if now - c.used > IDLE_CLOSE]
        conns = [CONNS.pop(k) for k in idle]
    for c in conns:
        c.close()


def state() -> dict:
    close_idle()
    servers = load()
    return {"servers": [{**public(s), "status": STATS.get(s["id"]) or {"state": "idle"}} for s in servers],
            "ssh_hosts": ssh_config_hosts(), "keys": key_files(), "terminal": bool(_ssh_exe())}


# ------------------------------------------------------------------ 解析状态脚本的输出
def _sections(out: str) -> dict[str, list[str]]:
    sec: dict[str, list[str]] = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("@") and re.fullmatch(r"@\w+", line.strip()):
            cur = line.strip()[1:]
            sec[cur] = []
        elif cur:
            sec[cur].append(line)
    return sec


def _cpu(line: str) -> tuple[int, int]:
    v = [int(x) for x in line.split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v[:8]), idle


def _net(lines: list[str]) -> tuple[int, int]:
    rx = tx = 0
    for ln in lines:
        if ":" not in ln:
            continue
        name, data = ln.split(":", 1)
        name = name.strip()
        if name == "lo" or name.startswith(("veth", "docker", "br-", "virbr", "cni", "flannel", "tun", "wg")):
            continue
        f = data.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
    return rx, tx


def _top(lines: list[str]) -> list[dict]:
    """top 第二轮的进程表：按表头找 PID / %CPU / %MEM / COMMAND 列（procps 和 busybox 的 top 列顺序不同）。"""
    lines = [ln for ln in lines if ln.strip()]
    if not lines or "PID" not in lines[0]:
        return []
    head = lines[0].split()
    try:
        ip, ic, icmd = head.index("PID"), head.index("%CPU"), len(head) - 1
        im = head.index("%MEM") if "%MEM" in head else None
    except ValueError:
        return []
    out = []
    for ln in lines[1:]:
        f = ln.split(None, icmd)
        if len(f) <= icmd:
            continue
        name = f[icmd].strip()
        if name in ("top", "awk", "sed", "head") or name.startswith("top "):
            continue  # 状态脚本自己
        try:
            cpu = float(f[ic].rstrip("%"))
            mem = float(f[im].rstrip("%")) if im is not None else None
        except ValueError:
            continue
        if cpu < 0.1:
            continue  # 闲着的不列
        out.append({"pid": f[ip], "cpu": cpu, "mem": mem, "name": name.split()[0].split("/")[-1][:40] or name})
    return out[:5]


def parse_stats(out: str) -> dict:
    sec = _sections(out)
    one = lambda k: (sec.get(k) or [""])[0].strip()  # noqa: E731
    st: dict = {"host": one("host"), "os": one("os"), "arch": one("arch")}
    try:
        t1, t2 = float(one("t1").split()[0]), float(one("t2").split()[0])
        st["uptime"] = int(t2)
        dt = max(t2 - t1, 0.1)
    except Exception:
        st["uptime"], dt = None, 0.5
    try:
        la = one("load").split()
        st["load"] = [float(x) for x in la[:3]]
    except Exception:
        st["load"] = None
    try:
        st["ncpu"] = int(one("ncpu"))
    except Exception:
        st["ncpu"] = None
    try:
        t1, i1 = _cpu(one("cpu1"))
        t2, i2 = _cpu(one("cpu2"))
        st["cpu"] = round(100 * (1 - (i2 - i1) / (t2 - t1)), 1) if t2 > t1 else 0.0
    except Exception:
        st["cpu"] = None
    try:
        r1, x1 = _net(sec.get("net1", []))
        r2, x2 = _net(sec.get("net2", []))
        st["net"] = {"rx": max(0, int((r2 - r1) / dt)), "tx": max(0, int((x2 - x1) / dt))}  # 每秒字节
    except Exception:
        st["net"] = None
    mem = {}
    for ln in sec.get("mem", []):
        m = re.match(r"(\w+):\s+(\d+)", ln)
        if m:
            mem[m.group(1)] = int(m.group(2)) * 1024
    if mem.get("MemTotal"):
        st["mem"] = {"total": mem["MemTotal"], "used": mem["MemTotal"] - mem.get("MemAvailable", 0)}
        if mem.get("SwapTotal"):
            st["swap"] = {"total": mem["SwapTotal"], "used": mem["SwapTotal"] - mem.get("SwapFree", 0)}
    disks, seen = [], set()
    for ln in sec.get("disk", []):
        f = ln.split()
        if len(f) < 6 or not f[1].isdigit():
            continue
        fs, mount = f[0], " ".join(f[5:])
        if fs in SKIP_FS or fs.startswith("/dev/loop") or SKIP_MOUNT.match(mount) or fs in seen:
            continue
        total = int(f[1]) * 1024
        if total < 512 * 1024 * 1024:
            continue
        seen.add(fs)
        disks.append({"mount": mount, "total": total, "used": int(f[2]) * 1024})
    disks.sort(key=lambda d: (d["mount"] != "/", -d["total"]))
    st["disks"] = disks[:4]
    st["top"] = _top(sec.get("top", []))
    try:
        st["users"] = int(one("users"))
    except Exception:
        st["users"] = None
    st["failed"] = [ln.split()[0] for ln in sec.get("failed", []) if ln.strip()]
    docker = []
    for ln in sec.get("docker", []):
        p = ln.split("|", 2)
        if len(p) == 3:
            docker.append({"name": p[0], "state": p[1], "status": p[2]})
    st["docker"] = docker
    return st


# ------------------------------------------------------------------ 打开完整的 SSH 终端
def _ssh_exe() -> str | None:
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    for p in (Path(sysroot) / "System32" / "OpenSSH" / "ssh.exe", Path(sysroot) / "Sysnative" / "OpenSSH" / "ssh.exe"):
        if p.exists():
            return str(p)
    return shutil.which("ssh")


def _windows_app(name: str) -> str | None:
    """应用商店装的程序（如 wt.exe）在 %LOCALAPPDATA%\\Microsoft\\WindowsApps，有时不在 PATH 里。"""
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / name
    return str(p) if os.environ.get("LOCALAPPDATA") and p.exists() else None


def open_terminal(sid: str) -> dict:
    s = next((x for x in load() if x["id"] == sid), None)
    if not s:
        return {"ok": False, "error": "没有这台服务器"}
    ssh = _ssh_exe()
    if not ssh:
        return {"ok": False, "error": "这台电脑没有 OpenSSH 客户端：设置 → 系统 → 可选功能 → 添加「OpenSSH 客户端」"}
    if s.get("alias"):
        args = [ssh, s["alias"]]
    else:
        args = [ssh, "-p", str(s.get("port") or 22)]
        if s.get("auth") == "key" and s.get("key"):
            args += ["-i", os.path.expanduser(s["key"])]
        args.append(f"{s['user']}@{s['host']}")
    wt = shutil.which("wt") or _windows_app("wt.exe")
    try:
        if wt:  # Windows Terminal：开在已有窗口的新标签页里
            subprocess.Popen([wt, "-w", "0", "new-tab", "--title", s["name"], *args], close_fds=True)
        else:   # 没装 Windows Terminal：新开一个控制台窗口
            subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
    except Exception as e:
        return {"ok": False, "error": f"打不开终端：{e}"}
    return {"ok": True, "password": s.get("auth") == "password"}


def summary(s: dict, st: dict | None) -> str:
    """一段文字描述（给 Bot 看）。"""
    head = f"{s['name']}  {s['user']}@{s['host']}:{s.get('port') or 22}"
    if not st:
        return head + "  （还没读到状态）"
    x = st.get("stats") or {}
    if st.get("state") == "error":
        head += f"  连接出错：{st.get('error')}"
        if not x:
            return head
    gb = lambda b: f"{b / 1073741824:.1f}G"  # noqa: E731
    parts = [x.get("os") or ""]
    if x.get("uptime") is not None:
        parts.append(f"已运行 {x['uptime'] // 86400} 天 {x['uptime'] % 86400 // 3600} 小时")
    if x.get("cpu") is not None:
        parts.append(f"CPU {x['cpu']}%（{x.get('ncpu') or '?'} 核，负载 {' '.join(map(str, x.get('load') or []))}）")
    if x.get("mem"):
        parts.append(f"内存 {gb(x['mem']['used'])}/{gb(x['mem']['total'])}")
    for d in x.get("disks") or []:
        parts.append(f"磁盘 {d['mount']} {gb(d['used'])}/{gb(d['total'])}")
    if x.get("net"):
        parts.append(f"网速 ↓{x['net']['rx'] // 1024}KB/s ↑{x['net']['tx'] // 1024}KB/s")
    lines = [head, "  " + "；".join(p for p in parts if p)]
    if x.get("top"):
        lines.append("  最忙的进程：" + "，".join(f"{t['name']}({t['cpu']}%)" for t in x["top"]))
    if x.get("failed"):
        lines.append("  失败的服务：" + "，".join(x["failed"]))
    if x.get("docker"):
        lines.append("  Docker：" + "，".join(f"{d['name']}({d['state']})" for d in x["docker"]))
    return "\n".join(lines)
