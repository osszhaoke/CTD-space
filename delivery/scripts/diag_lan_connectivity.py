#!/usr/bin/env python3
"""StarMap C01 · 局域网访问与跨机连通性一键诊断工具
针对同网段其他设备（手机、iPad、其他电脑）无法访问网页进行全方位排查。
"""
import datetime as dt
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import urllib.request
import urllib.error

REPORT_DIR = Path.home() / "Desktop"

def get_lan_ips():
    ips = []
    # 优先取物理 Wi-Fi / 以太网网卡 (en0, en1 等)，过滤 utun/vpn 等虚拟网卡
    for iface in ("en0", "en1", "en2", "en3", "en4"):
        try:
            res = subprocess.check_output(["ipconfig", "getifaddr", iface], text=True, stderr=subprocess.DEVNULL).strip()
            if res and not res.startswith("127."):
                ips.append((iface, res))
        except Exception:
            pass
    # 辅助兜底：如果 enX 没取到，再尝试通过网络连接探测真实物理出站 IP
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            sock_ip = s.getsockname()[0]
            s.close()
            # 排除 198.18.x.x 代理保留地址及 127.x.x.x
            if sock_ip and not sock_ip.startswith("198.18.") and not sock_ip.startswith("127."):
                ips.append(("primary", sock_ip))
        except Exception:
            pass
    return ips

def check_listening_service(port=8765):
    res = {
        "is_listening": False,
        "pid": None,
        "command": None,
        "listen_host": None,
    }
    try:
        out = subprocess.check_output(["lsof", f"-iTCP:{port}", "-sTCP:LISTEN"], text=True, stderr=subprocess.DEVNULL)
        lines = [line.strip() for line in out.strip().split("\n") if line.strip()]
        if len(lines) > 1:
            parts = lines[1].split()
            res["is_listening"] = True
            res["command"] = parts[0]
            res["pid"] = parts[1]
            for p in parts:
                if ":" in p and (p.startswith("*:") or p.startswith("0.0.0.0:") or p.startswith("127.0.0.1:") or p.startswith("[::]:")):
                    res["listen_host"] = p
    except Exception:
        pass
    return res

def check_macos_firewall():
    res = {"state": "UNKNOWN", "detail": ""}
    try:
        out = subprocess.check_output(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"], text=True, stderr=subprocess.DEVNULL)
        res["detail"] = out.strip()
        if "enabled" in out.lower():
            res["state"] = "ENABLED (可能拦截入站连接)"
        elif "disabled" in out.lower():
            res["state"] = "DISABLED (已关闭，不拦截)"
    except Exception as e:
        res["detail"] = str(e)
    return res

def test_http_endpoint(host, port=8765, path="/healthz"):
    url = f"http://{host}:{port}{path}"
    headers = {"Host": f"{host}:{port}"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return {"status": resp.status, "ok": resp.status == 200, "error": None}
    except urllib.error.HTTPError as e:
        code, reason = e.code, e.reason
        e.close()
        return {"status": code, "ok": False, "error": f"HTTP {code}: {reason}"}
    except Exception as e:
        return {"status": None, "ok": False, "error": f"{type(e).__name__}: {e}"}

def main():
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp_slug = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = REPORT_DIR / f"CTDS-LAN-DIAG-{timestamp_slug}.txt"

    lines = []
    def log(msg="", indent=0):
        prefix = "  " * indent
        line = f"{prefix}{msg}"
        print(line)
        lines.append(line)

    log("============================================================")
    log("  StarMap C01 · 局域网跨机访问连通性诊断报告")
    log(f"  测试时间: {now_str}")
    log(f"  本机系统: macOS {platform.mac_ver()[0]} ({platform.machine()})")
    log("============================================================")

    # 1. 检查服务监听
    log("[1] 检查大屏看板服务 (端口 8765) 监听状态...")
    srv = check_listening_service(8765)
    if not srv["is_listening"]:
        log("❌ 服务未在运行！端口 8765 没有进程监听。", 1)
        log("   原因：后台服务已被退出或未启动。", 1)
        log("   解决方案：双击运行「一键部署_StarMap_局域网共享.command」重新拉起服务。", 1)
    else:
        log(f"✅ 服务运行中: PID={srv['pid']}, 进程={srv['command']}", 1)
        log(f"   监听地址: {srv['listen_host']}", 1)
        if srv["listen_host"] and "127.0.0.1" in srv["listen_host"]:
            log("⚠️ 警告：当前服务仅绑定了 127.0.0.1 (本地环回)，未开启局域网共享！", 1)
            log("   其他设备将无法连接。请务必双击「一键部署_StarMap_局域网共享.command」启动。", 1)
        else:
            log("✅ 监听地址为全局通配 (*:8765 或 0.0.0.0:8765)，支持局域网设备连接。", 1)

    # 2. 检查本机局域网 IP
    log("\n[2] 本机局域网 IP 地址获取...")
    lan_ips = get_lan_ips()
    if not lan_ips:
        log("❌ 未检测到有效的 Wi-Fi 或以太网局域网 IPv4 地址！", 1)
        log("   请确认本机已连接 Wi-Fi 或网线。", 1)
    else:
        for iface, ip in lan_ips:
            log(f"✅ 发现可用网卡 [{iface}]: {ip}", 1)

    # 3. 本地与局域网 HTTP 响应测试
    log("\n[3] HTTP 服务端安全放行与响应测试...")
    loopback_test = test_http_endpoint("127.0.0.1")
    if loopback_test["ok"]:
        log("✅ 127.0.0.1 环回测试: 正常 (HTTP 200 OK)", 1)
    else:
        log(f"❌ 127.0.0.1 环回测试失败: {loopback_test['error']}", 1)

    accessible_urls = []
    for iface, ip in lan_ips:
        lan_test = test_http_endpoint(ip)
        if lan_test["ok"]:
            log(f"✅ 模拟局域网访问 (IP: {ip}): 放行成功 (HTTP 200 OK)", 1)
            accessible_urls.append(f"http://{ip}:8765/")
        else:
            log(f"❌ 模拟局域网访问 (IP: {ip}) 失败: {lan_test['error']}", 1)

    # 4. macOS 系统防火墙检查
    log("\n[4] macOS 系统防火墙状态检查...")
    fw = check_macos_firewall()
    log(f"   防火墙状态: {fw['state']}", 1)
    if "ENABLED" in fw["state"]:
        log("⚠️ 提示：macOS 系统防火墙处于开启状态，某些网络配置下可能会静默阻断入站 8765 端口。", 1)
        log("   排查建议：打开「系统设置」->「网络」->「防火墙」，查看是否阻断了 Python 程序的入站连接。", 1)
    else:
        log("✅ macOS 防火墙未开启，不存在本地防火墙拦截。", 1)

    # 5. 跨机连通性总结与使用建议
    log("\n============================================================")
    log("  【跨设备访问结论与指导】")
    log("============================================================")
    if accessible_urls:
        log("🌟 局域网服务运行正常！请在其他电脑/手机/iPad 上按如下步骤访问：")
        log("1. 确保其他设备连接的是【同一个 Wi-Fi】名称（不要一个连 5G、另一个连访客 Wi-Fi 或手机热点）。")
        log("2. 确保路由器没有开启【AP 隔离 / 客户端隔离 (AP Isolation)】功能。")
        log("3. 在其他设备的浏览器中输入以下地址（建议直接复制或扫描二维码）：")
        for u in accessible_urls:
            log(f"   👉  {u}")
    else:
        log("❌ 目前局域网无法访问。主要可能原因：")
        if not srv["is_listening"]:
            log("   - 大屏服务未启动，请双击运行「一键部署_StarMap_局域网共享.command」。")
        elif srv["listen_host"] and "127.0.0.1" in srv["listen_host"]:
            log("   - 服务是以纯本地模式启动的，请双击「一键部署_StarMap_局域网共享.command」切换为局域网模式。")
        else:
            log("   - 可能是网络策略或 Host 防御阻断，请将本诊断报告发回分析。")
    log("============================================================")

    try:
        out_file.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n📄 完整诊断报告已保存至桌面: {out_file.name}")
        subprocess.run(["open", "-R", str(out_file)], check=False)
    except Exception as e:
        print(f"\n(写入报告文件异常: {e})")

if __name__ == "__main__":
    main()
