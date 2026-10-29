#!/usr/bin/env python3
"""StarMap C01 · Eight Sleep 贯通性与环境诊断脚本

功能：
1. 收集本地环境（macOS 版本、Python 运行时、默认网关、DNS 解析、代理环境变量）；
2. 逐一诊断 Eight Sleep 官方域名（auth-api, client-api, app-api）的 DNS 解析、TCP 握手、TLS/SSL 证书链及往返延迟；
3. 基于当前包内配置（var/ctds-c01/config.json）静默测试各 API 端点连通性（Token 有效期、各接口 HTTP 状态码、响应体字节数）；
4. 提取最近 40 行运行日志（自动执行严格脱敏：绝不输出密码、Token、邮箱或用户 UUID）；
5. 生成单个诊断报告文件，并在访达（Finder）中高亮定位，方便通过隔空投送（AirDrop）回传分析。

安全底线：严格脱敏，不输出任何凭据与个人真实生理读数。
"""
import datetime as dt
import hashlib
import json
import os
import platform
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = ROOT / "development/ctds"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

EIGHT_SLEEP_HOSTS = [
    ("auth-api.8slp.net", "认证与 Token 刷新服务"),
    ("client-api.8slp.net", "核心用户/设备/趋势服务"),
    ("app-api.8slp.net", "实时温控与底座升降服务"),
]


def get_ssl_context():
    """Reuse room_display.adapter.get_ssl_context (with standalone fallback if run outside repo)."""
    try:
        from room_display import adapter as _adapter
        return _adapter.get_ssl_context()
    except Exception:
        ctx = ssl.create_default_context()
        for path in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
            if os.path.isfile(path):
                try:
                    ctx.load_verify_locations(path)
                    return ctx
                except Exception:
                    pass
        return ctx


def mask_string(s: str, keep_start: int = 2, keep_end: int = 2) -> str:
    if not s or not isinstance(s, str):
        return "—"
    if len(s) <= keep_start + keep_end:
        return "***"
    return f"{s[:keep_start]}***{s[-keep_end:]}"

def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "—"
    name, domain = email.split("@", 1)
    masked_name = name[0] + "***" if len(name) > 1 else "*"
    return f"{masked_name}@{domain}"

def mask_log_line(line: str) -> str:
    # 遮蔽 Bearer token、密码、邮箱、UUID（不泄露完整邮箱前缀或完整 36 位 UUID）
    line = re.sub(r"Bearer\s+[a-zA-Z0-9_\-\.]{20,}", "Bearer [MASKED_TOKEN]", line)
    line = re.sub(r'("password"\s*:\s*")[^"]+(")', r'\1[MASKED]\2', line)
    line = re.sub(
        r"([a-zA-Z0-9_.+-]+)@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)",
        lambda m: mask_email(m.group(0)),
        line,
    )
    line = re.sub(
        r"([0-9a-fA-F]{8})-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        r"\1-****-****-****-************",
        line,
    )
    return line

def get_sys_info():
    info = {
        "macos_version": "未知",
        "arch": platform.machine(),
        "python_version": sys.version.split()[0],
        "python_path": sys.executable,
        "local_time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S %z"),
        "gateway": "未知",
        "dns_servers": [],
        "proxy_vars": {},
    }
    try:
        ver = platform.mac_ver()[0]
        if ver:
            info["macos_version"] = f"macOS {ver} ({platform.machine()})"
    except Exception:
        pass

    # 默认网关
    try:
        res = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=3)
        for line in res.stdout.splitlines():
            if "gateway:" in line:
                info["gateway"] = line.split("gateway:")[1].strip()
            elif "interface:" in line:
                info["interface"] = line.split("interface:")[1].strip()
    except Exception:
        pass

    # DNS
    try:
        if os.path.exists("/etc/resolv.conf"):
            with open("/etc/resolv.conf", "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("nameserver"):
                        info["dns_servers"].append(line.split()[1].strip())
    except Exception:
        pass

    # 代理环境变量（遮蔽密码）
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        val = os.environ.get(k)
        if val:
            parsed = urllib.parse.urlsplit(val)
            if parsed.password:
                netloc = f"{parsed.username}:***@{parsed.hostname}"
                if parsed.port:
                    netloc += f":{parsed.port}"
                val = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
            info["proxy_vars"][k] = val
    return info

def check_host_ssl(host: str, port: int = 443):
    res = {
        "host": host,
        "dns_ok": False,
        "dns_ms": None,
        "resolved_ips": [],
        "tcp_ok": False,
        "tcp_ms": None,
        "ssl_ok": False,
        "ssl_version": None,
        "cipher": None,
        "cert_issuer": None,
        "cert_expiry": None,
        "error": None,
        "ssl_kind": None,
    }
    # 1. DNS
    t0 = time.time()
    try:
        addrs = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        res["dns_ms"] = round((time.time() - t0) * 1000, 1)
        res["dns_ok"] = True
        res["resolved_ips"] = sorted(list({a[4][0] for a in addrs}))
    except Exception as exc:
        res["error"] = f"DNS 解析失败: {type(exc).__name__} - {exc}"
        return res

    target_ip = res["resolved_ips"][0]

    # 2. TCP
    t1 = time.time()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    try:
        sock.connect((target_ip, port))
        res["tcp_ms"] = round((time.time() - t1) * 1000, 1)
        res["tcp_ok"] = True
    except Exception as exc:
        sock.close()
        res["error"] = f"TCP 连接失败 ({target_ip}:{port}): {type(exc).__name__} - {exc}"
        return res

    # 3. SSL / TLS
    try:
        ctx = get_ssl_context()
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            res["ssl_ok"] = True
            res["ssl_version"] = ssock.version()
            res["cipher"] = ssock.cipher()[0] if ssock.cipher() else "未知"
            cert = ssock.getpeercert()
            if cert:
                res["cert_expiry"] = cert.get("notAfter")
                issuer = cert.get("issuer", ())
                for rdn in issuer:
                    for k, v in rdn:
                        if k == "organizationName":
                            res["cert_issuer"] = v
    except Exception as exc:
        sock.close()
        res["error"] = f"TLS/SSL 握手失败: {type(exc).__name__} - {exc}"
        res["ssl_kind"] = classify_tls_error(exc)
    return res


def classify_tls_error(exc) -> str:
    """MISSING_LOCAL_CA: this Mac's Python lacks root certificates (fixable locally).
    UNTRUSTED_CHAIN: the server chain itself is not trusted (self-signed / hostname mismatch / interception)."""
    code = getattr(exc, "verify_code", None)
    msg = str(exc)
    if code == 20 or "unable to get local issuer certificate" in msg:
        return "MISSING_LOCAL_CA"
    if isinstance(exc, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in msg:
        return "UNTRUSTED_CHAIN"
    return "OTHER"


def python_root_ca_count() -> int:
    """Root CAs visible to this Python's *default* context (0 = python.org build without certificates installed)."""
    try:
        return ssl.create_default_context().cert_store_stats().get("x509_ca", 0)
    except Exception:
        return -1


def install_certificates_hint() -> str:
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    cmd = Path(f"/Applications/Python {ver}/Install Certificates.command")
    if cmd.is_file():
        return f'open "{cmd}"'
    return "安装 certifi：python3 -m pip install --upgrade certifi"

def test_api_endpoints(cfg_path: Path):
    diag = {
        "config_found": False,
        "mode": "未知",
        "has_credentials": False,
        "credentials_summary": {},
        "api_results": [],
    }
    if not cfg_path.is_file():
        return diag

    diag["config_found"] = True
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        diag["config_error"] = str(exc)
        return diag

    es = cfg.get("eight_sleep") or {}
    diag["mode"] = es.get("mode", "READY")
    bearer_token = es.get("bearer_token") or ""
    refresh_token = es.get("refresh_token") or ""
    user_id = es.get("user_id") or ""
    device_id = es.get("device_id") or ""
    email = es.get("email") or ""
    has_pwd = bool(es.get("password"))
    token_exp = es.get("token_expires_at")

    token_exp_str = "未知"
    is_exp = None
    if token_exp:
        if isinstance(token_exp, (int, float)):
            token_exp_str = dt.datetime.fromtimestamp(token_exp, dt.timezone.utc).isoformat()
            is_exp = time.time() > token_exp
        else:
            token_exp_str = str(token_exp)
            try:
                exp_ts = dt.datetime.fromisoformat(str(token_exp).replace("Z", "+00:00")).timestamp()
                is_exp = time.time() > exp_ts
            except Exception:
                pass

    diag["has_credentials"] = bool(bearer_token or refresh_token or (email and has_pwd))
    diag["credentials_summary"] = {
        "email": mask_email(email),
        "has_password": has_pwd,
        "user_id": mask_string(user_id, 4, 4),
        "device_id": mask_string(device_id, 4, 4),
        "bearer_token_present": bool(bearer_token),
        "bearer_token_len": len(bearer_token) if bearer_token else 0,
        "refresh_token_present": bool(refresh_token),
        "token_expires_at": token_exp_str,
        "is_expired": is_exp,
    }

    if not bearer_token:
        return diag

    # 逐一测试端点
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json",
        "User-Agent": "StarMap-Diagnostics/1.0",
    }

    endpoints = [
        ("client-api", "users/me", f"https://client-api.8slp.net/v1/users/me"),
    ]
    if user_id:
        today = dt.date.today().isoformat()
        endpoints.append(("client-api", "trends", f"https://client-api.8slp.net/v1/users/{user_id}/trends?tz=Asia/Shanghai&from={today}&to={today}&include-main=false&include-all-sessions=true&model-version=v2"))
        endpoints.append(("app-api", "temperature", f"https://app-api.8slp.net/v1/users/{user_id}/temperature"))
        endpoints.append(("app-api", "base", f"https://app-api.8slp.net/v1/users/{user_id}/base"))
    if device_id:
        endpoints.append(("client-api", "device", f"https://client-api.8slp.net/v1/devices/{device_id}?filter=leftHeatingLevel,rightHeatingLevel"))

    for svc, label, url in endpoints:
        item = {
            "service": svc,
            "label": label,
            "status_code": None,
            "latency_ms": None,
            "payload_bytes": None,
            "outcome": "UNKNOWN",
            "detail": None,
        }
        t0 = time.time()
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=8.0, context=get_ssl_context()) as resp:
                data = resp.read()
                item["status_code"] = resp.status
                item["latency_ms"] = round((time.time() - t0) * 1000, 1)
                item["payload_bytes"] = len(data)
                item["outcome"] = "OK"
        except urllib.error.HTTPError as exc:
            item["status_code"] = exc.code
            item["latency_ms"] = round((time.time() - t0) * 1000, 1)
            item["outcome"] = f"HTTP_{exc.code}"
            item["detail"] = f"HTTP {exc.code} {exc.reason}"
            exc.close()
        except Exception as exc:
            item["latency_ms"] = round((time.time() - t0) * 1000, 1)
            item["outcome"] = "NO_CONNECTION"
            item["detail"] = f"{type(exc).__name__}: {exc}"
        diag["api_results"].append(item)

    return diag

def get_recent_logs(max_lines: int = 35) -> list:
    out = []
    log_paths = [
        ROOT / "var/ctds-c01/daemon.log",
        ROOT / "var/ctds-c01/collector.log",
    ]
    for p in log_paths:
        if p.is_file():
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                recent = lines[-max_lines:]
                mtime = dt.datetime.fromtimestamp(p.stat().st_mtime)
                age_h = (dt.datetime.now() - mtime).total_seconds() / 3600
                stale = f"  ⚠ 已 {age_h:.1f} 小时未更新，为历史记录，不代表当前状态" if age_h > 1 else ""
                out.append(f"--- 最近日志片段: {p.name} (末尾 {len(recent)} 行，最后更新 {mtime:%Y-%m-%d %H:%M}){stale} ---")
                for l in recent:
                    out.append(mask_log_line(l))
            except Exception as exc:
                out.append(f"读取 {p.name} 失败: {exc}")
    return out

def run_diagnostics():
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_lines = []
    w = report_lines.append

    w("=" * 66)
    w("       StarMap C01 · Eight Sleep 贯通性与运行环境诊断报告")
    w("=" * 66)
    w(f"生成时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (本机时间)")
    w(f"报告 ID:   CTDS-ES-DIAG-{ts}")
    w("")

    # 1. 系统与网络环境
    w("【一、系统与本地网络环境】")
    sys_info = get_sys_info()
    w(f"  • 操作系统:     {sys_info['macos_version']}")
    w(f"  • Python 运行时: {sys_info['python_version']} ({sys_info['python_path']})")
    w(f"  • 本机时区时间: {sys_info['local_time']}")
    w(f"  • 默认网关/网卡: {sys_info['gateway']} ({sys_info.get('interface', '未知')})")
    w(f"  • 系统 DNS 服务器: {', '.join(sys_info['dns_servers']) if sys_info['dns_servers'] else '未明确配置'}")
    if sys_info['proxy_vars']:
        w("  • 检测到网络代理环境变量:")
        for k, v in sys_info['proxy_vars'].items():
            w(f"      - {k} = {v}")
    else:
        w("  • 系统网络代理: 未设置 HTTP/HTTPS 代理环境变量（直连模式）")
    w("")

    # 2. Eight Sleep 官方各域名网络层探测
    w("【二、Eight Sleep 官方域名网络层探测】")
    host_results = []
    for host, desc in EIGHT_SLEEP_HOSTS:
        res = check_host_ssl(host)
        host_results.append(res)
        w(f"  ▶ 目标服务: {host} ({desc})")
        if res["dns_ok"]:
            w(f"      [DNS 解析] 成功 ({res['dns_ms']} ms) → {', '.join(res['resolved_ips'][:3])}")
        else:
            w(f"      [DNS 解析] 失败: {res['error']}")
        if res["tcp_ok"]:
            w(f"      [TCP 连接] 成功 (443 端口往返 {res['tcp_ms']} ms)")
        else:
            w(f"      [TCP 连接] 失败: {res['error']}")
        if res["ssl_ok"]:
            w(f"      [TLS 握手] 成功 (协议: {res['ssl_version']}, 加密套件: {res['cipher']})")
            w(f"      [证书信息] 签发机构: {res['cert_issuer']} | 有效期至: {res['cert_expiry']}")
        else:
            w(f"      [TLS 握手] 失败: {res['error']}")
        w("")

    # 3. 业务 API 与凭据有效性探测
    w("【三、Eight Sleep 凭据与业务接口贯通性】")
    cfg_p = ROOT / "var/ctds-c01/config.json"
    api_diag = test_api_endpoints(cfg_p)
    if not api_diag["config_found"]:
        w(f"  • 未找到配置文件: {cfg_p}")
    else:
        w(f"  • 运行模式: {api_diag['mode']}")
        cs = api_diag["credentials_summary"]
        w(f"  • 凭据状态: {'已配置' if api_diag['has_credentials'] else '未配置'}")
        w(f"      - 注册邮箱:   {cs.get('email')}")
        w(f"      - 用户标识:   {cs.get('user_id')}")
        w(f"      - 设备标识:   {cs.get('device_id')}")
        w(f"      - 访问令牌:   {'已存在 (有效长度 ' + str(cs.get('bearer_token_len')) + ')' if cs.get('bearer_token_present') else '不存在'}")
        w(f"      - 令牌到期:   {cs.get('token_expires_at')} (是否已过期: {cs.get('is_expired')})")
        w("")
        if api_diag["api_results"]:
            w("  • 接口实时调用探针测试:")
            for item in api_diag["api_results"]:
                status_str = f"HTTP {item['status_code']}" if item['status_code'] else item['outcome']
                lat_str = f"{item['latency_ms']} ms" if item['latency_ms'] else "—"
                size_str = f"{item['payload_bytes']} B" if item['payload_bytes'] is not None else "0 B"
                w(f"      [{item['outcome']}] {item['service']} · {item['label']:<12} | 状态: {status_str:<9} | 耗时: {lat_str:<8} | 响应体: {size_str}")
                if item["detail"]:
                    w(f"          原因/详情: {item['detail']}")
        else:
            w("  • 未执行业务接口探针测试（原因：包内无可用 Bearer 令牌）。")
    w("")

    # 4. 运行日志片段
    w("【四、最近守护进程运行日志片段（已脱敏）】")
    logs = get_recent_logs(35)
    if logs:
        for l in logs:
            w(f"  {l}")
    else:
        w("  （未找到本地 daemon.log 或 collector.log）")
    w("")

    # 5. 总结与建议
    w("【五、综合诊断评估】")
    dns_fails = [h["host"] for h in host_results if not h["dns_ok"]]
    tcp_fails = [h["host"] for h in host_results if not h["tcp_ok"]]
    ssl_fails = [h["host"] for h in host_results if not h["ssl_ok"]]
    api_fails = [r for r in api_diag.get("api_results", []) if r["outcome"] != "OK"]

    if dns_fails:
        w(f"  [严重警告] 存在 DNS 域名解析失败: {', '.join(dns_fails)}。请检查本地 DNS 设置或 Wi-Fi 是否限制访问外网。")
    elif tcp_fails:
        w(f"  [严重警告] 无法与 Eight Sleep 服务器建立 TCP 连接: {', '.join(tcp_fails)}。可能受到路由器防火墙拦截。")
    elif ssl_fails:
        kinds = {h.get("ssl_kind") for h in host_results if not h["ssl_ok"]}
        if kinds == {"MISSING_LOCAL_CA"}:
            w(f"  [严重警告] 本机 Python 缺少根证书，无法校验 Eight Sleep 证书: {', '.join(ssl_fails)}。")
            w(f"      这不是网络或账号问题。修复：在终端运行 {install_certificates_hint()} ，然后重新一键部署。")
        else:
            w(f"  [严重警告] TLS/SSL 证书校验失败: {', '.join(ssl_fails)}。服务器证书链不被信任，可能存在代理/证书拦截（公司网络、安全软件）。")
    elif api_fails:
        auth_errs = [r for r in api_fails if r["status_code"] in (401, 403)]
        if auth_errs:
            w("  [警告] 业务接口返回认证失败 (HTTP 401/403)。当前 Token 可能已在服务器端被注销或需重新绑定。")
        else:
            w(f"  [提示] 部分业务接口返回异常状态码: {', '.join(r['label'] + ':' + str(r['status_code']) for r in api_fails)}。")
    else:
        w("  [结论: 优良] 与 Eight Sleep 全部官方服务端点网络物理连通、TLS 握手及业务接口调用均 100% 畅通！")
    if python_root_ca_count() == 0:
        w(f"  [提示] 本机 Python 默认未安装根证书（服务已自动改用 macOS 系统根证书）。建议运行 {install_certificates_hint()} 。")
    w("=" * 66)

    report_text = "\n".join(report_lines)

    # 寻找最佳输出路径（首选桌面，方便 AirDrop）
    desktop = Path.home() / "Desktop"
    target_dir = desktop if desktop.is_dir() else (ROOT / "var/ctds-c01")
    target_dir.mkdir(parents=True, exist_ok=True)
    report_file = target_dir / f"StarMap_EightSleep_诊断报告_{ts}.txt"
    report_file.write_text(report_text, encoding="utf-8")
    os.chmod(report_file, 0o644)

    print(report_text)
    print("\n" + "#" * 66)
    print(f"  ✅ 诊断报告已生成至:")
    print(f"     {report_file.resolve()}")
    print("  📲 正在打开访达 (Finder) 并定位该文件...")
    print("     请在访达中右键该文件 -> 共享 -> 隔空投送 (AirDrop) 发回！")
    print("#" * 66 + "\n")

    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(report_file.resolve())], check=False)

    return 0

if __name__ == "__main__":
    sys.exit(run_diagnostics())
