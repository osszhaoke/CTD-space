"""LAN AirGradient discovery and self-updating connection binding.

Probes local network candidates for a valid AirGradient /measures/current JSON
payload, updates private runtime config (var/ctds-c01/config.json) when found,
and never logs or persists hardware identifiers (serialno/MAC/raw IP in public output).
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import json
import re
import socket
import subprocess

from . import adapter, catalog, config

AG_SIGNATURE_KEYS = frozenset({
    "pm02", "rco2", "atmp", "rhum", "pm01", "pm10", "tvocIndex", "noxIndex", "pm003Count"
})
_ARP_IP_RE = re.compile(r"\((\d{1,3}(?:\.\d{1,3}){3})\)")
_IFCONFIG_INET_RE = re.compile(r"inet\s+(\d{1,3}(?:\.\d{1,3}){3})\s+netmask")


def is_airgradient_payload(doc):
    if type(doc) is not dict:
        return False
    matched = sum(1 for k in AG_SIGNATURE_KEYS if k in doc)
    return matched >= 2


def probe_base_url(base_url, *, timeout=0.8, fetcher=None, clock=adapter.utc_now):
    try:
        adapter.validate_base_url(base_url)
    except adapter.AdapterError:
        return None
    do_fetch = fetcher or (lambda u, timeout=timeout: adapter.fetch(u, timeout=timeout, clock=clock))
    res = do_fetch(base_url, timeout=timeout)
    if res.get("outcome") != "OK" or res.get("http_status") != 200 or not res.get("body"):
        return None
    try:
        doc = adapter.strict_json(res["body"])
    except adapter.AdapterError:
        return None
    if not is_airgradient_payload(doc):
        return None
    metrics_count = sum(1 for m in catalog.METRICS.values() if m["field"] in doc)
    return {"base_url": base_url, "metrics_count": metrics_count}


def _is_private_unicast(ip_str):
    try:
        ip = ipaddress.IPv4Address(ip_str)
    except ipaddress.AddressValueError:
        return False
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local:
        return False
    last_octet = int(ip_str.rsplit(".", 1)[-1])
    if last_octet in (0, 255):
        return False
    return ip.is_private


def _arp_neighbor_ips():
    out = []
    try:
        proc = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=2.0, check=False)
        for m in _ARP_IP_RE.finditer(proc.stdout or ""):
            ip = m.group(1)
            if _is_private_unicast(ip) and ip not in out:
                out.append(ip)
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def _local_ipv4_addresses():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            primary = s.getsockname()[0]
            if _is_private_unicast(primary):
                ips.append(primary)
        finally:
            s.close()
    except OSError:
        pass
    try:
        proc = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=2.0, check=False)
        for m in _IFCONFIG_INET_RE.finditer(proc.stdout or ""):
            ip = m.group(1)
            if _is_private_unicast(ip) and ip not in ips:
                ips.append(ip)
    except (OSError, subprocess.SubprocessError):
        pass
    return ips


def local_candidate_batches(preferred_base_url=None):
    """Return [priority_batch, subnet_batch] of http://host candidate base URLs."""
    seen = set()
    priority = []
    if preferred_base_url:
        try:
            adapter.validate_base_url(preferred_base_url)
            priority.append(preferred_base_url)
            seen.add(preferred_base_url)
        except adapter.AdapterError:
            pass

    self_ips = set(_local_ipv4_addresses())
    for ip in _arp_neighbor_ips():
        if ip in self_ips:
            continue
        url = f"http://{ip}"
        if url not in seen:
            seen.add(url)
            priority.append(url)

    subnet_batch = []
    for my_ip in self_ips:
        prefix = my_ip.rsplit(".", 1)[0]
        for host in range(1, 255):
            ip = f"{prefix}.{host}"
            if ip in self_ips:
                continue
            url = f"http://{ip}"
            if url not in seen:
                seen.add(url)
                subnet_batch.append(url)
    return [b for b in (priority, subnet_batch) if b]


def _probe_batch(urls, *, timeout, max_workers, fetcher):
    if not urls:
        return None
    workers = max(1, min(max_workers, len(urls)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(probe_base_url, u, timeout=timeout, fetcher=fetcher): u for u in urls}
        for fut in as_completed(futs):
            res = fut.result()
            if res is not None:
                for other in futs:
                    other.cancel()
                return res
    return None


def discover_airgradient(*, preferred_base_url=None, candidates=None, timeout=0.8, max_workers=48, fetcher=None):
    """Discover an AirGradient device on the local network. Returns dict(base_url, metrics_count) or None."""
    if candidates is not None:
        if preferred_base_url and preferred_base_url not in candidates:
            first = probe_base_url(preferred_base_url, timeout=timeout, fetcher=fetcher)
            if first is not None:
                return first
        return _probe_batch(list(candidates), timeout=timeout, max_workers=max_workers, fetcher=fetcher)

    if preferred_base_url:
        fast = probe_base_url(preferred_base_url, timeout=timeout, fetcher=fetcher)
        if fast is not None:
            return fast

    for batch in local_candidate_batches(preferred_base_url=None):
        found = _probe_batch(batch, timeout=timeout, max_workers=max_workers, fetcher=fetcher)
        if found is not None:
            return found
    return None


def auto_bind(config_path=None, *, candidates=None, timeout=0.8, fetcher=None):
    """Discover AirGradient on LAN and self-update private config.json if found."""
    import os
    path = config.resolve_path(config_path) if config_path else config.DEFAULT_CONFIG
    existed = path.exists()
    if existed:
        raw_doc = json.loads(path.read_text(encoding="utf-8"))
        cfg = config.validate(raw_doc)
    else:
        cfg = dict(
            config.DEFAULTS,
            source=dict(config.DEFAULTS["source"]),
            eight_sleep=dict(config.DEFAULTS["eight_sleep"]),
        )

    changed = not existed
    if cfg.get("room_name_zh") == "示范房间":
        cfg["room_name_zh"] = config.DEFAULTS["room_name_zh"]
        changed = True

    es = cfg.setdefault("eight_sleep", dict(config.DEFAULTS["eight_sleep"]))
    if config.migrate_doc(cfg):
        changed = True

    preferred = cfg["source"].get("base_url")
    found = discover_airgradient(preferred_base_url=preferred, candidates=candidates,
                                 timeout=timeout, fetcher=fetcher)
    if found is not None:
        old_url = cfg["source"].get("base_url")
        old_mode = cfg["source"].get("mode")
        cfg["source"]["mode"] = "LIVE"
        cfg["source"]["base_url"] = found["base_url"]
        if existed and (old_url != found["base_url"] or old_mode != "LIVE"):
            cfg["source"]["binding_revision"] = int(cfg["source"].get("binding_revision", 1)) + 1
            changed = True
        elif not existed:
            changed = True

    if changed:
        config.save_config(cfg, path)

    return {
        "discovered": found is not None,
        "mode": cfg["source"]["mode"],
        "eight_sleep_mode": es.get("mode", "READY"),
        "binding_updated": changed,
        "binding_revision": cfg["source"]["binding_revision"],
        "metrics_count": found["metrics_count"] if found else 0,
        "config": cfg,
    }
