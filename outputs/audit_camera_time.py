import concurrent.futures
import csv
import datetime as dt
import getpass
import http.client
import ipaddress
import pathlib
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from probe_onvif_time import compact_fault, onvif_post, parse_ntp, parse_system_time


NETWORK_ERRORS = (
    urllib.error.URLError,
    urllib.error.HTTPError,
    TimeoutError,
    http.client.RemoteDisconnected,
    ConnectionError,
    OSError,
)

FIELDS = [
    "timestamp",
    "ip",
    "model",
    "mac",
    "discovery_protocol",
    "status",
    "source",
    "username",
    "ntp_enabled",
    "ntp_server",
    "timezone",
    "daylight_savings",
    "utc_time",
    "message",
]


def ping(ip: str, timeout_ms: int) -> bool:
    process = subprocess.run(
        ["ping", "-n", "1", "-w", str(timeout_ms), ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return process.returncode == 0


def parse_credential_rule(value: str) -> tuple[int, int, tuple[str, str]]:
    target, separator, credential = value.partition("=")
    if not separator or ":" not in credential:
        raise ValueError(f"Invalid --credential-for: {value}")
    username, password = credential.split(":", 1)
    start_text, dash, end_text = target.partition("-")
    start = int(ipaddress.IPv4Address(start_text.strip()))
    end = int(ipaddress.IPv4Address(end_text.strip())) if dash else start
    if end < start or not username:
        raise ValueError(f"Invalid --credential-for: {value}")
    return start, end, (username, password)


def credentials_for(ip: str, defaults: list[tuple[str, str]], rules: list[tuple[int, int, tuple[str, str]]]):
    value = int(ipaddress.IPv4Address(ip))
    matching = [credential for start, end, credential in rules if start <= value <= end]
    return matching or defaults


def parse_sunell_values(text: str) -> dict[str, str]:
    pattern = re.compile(r"([A-Za-z][A-Za-z0-9_]*)=(.*?)(?=\s+[A-Za-z][A-Za-z0-9_]*=|[\r\n;]|$)")
    return {match.group(1): match.group(2).strip() for match in pattern.finditer(text)}


def sunell_get(ip: str, username: str, password: str, timeout: float, info_type: str) -> str:
    query = urllib.parse.urlencode(
        {"userName": username, "password": password, "action": "get", "type": info_type}
    )
    with urllib.request.urlopen(f"http://{ip}/cgi-bin/param.cgi?{query}", timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def audit_sunell(ip: str, username: str, password: str, timeout: float) -> dict:
    result = {"source": "", "ntp_enabled": "", "ntp_server": "", "timezone": "", "message": ""}
    messages = []
    authenticated = False
    for info_type in ("NTP", "time", "dateTime", "timeConfig"):
        try:
            text = sunell_get(ip, username, password, timeout, info_type)
        except NETWORK_ERRORS as exc:
            messages.append(f"{info_type}: {exc}")
            continue
        lower = text.lower()
        if "error" in lower or "return=-" in lower:
            messages.append(f"{info_type}: {' '.join(text.split())[:160]}")
            continue
        authenticated = True
        values = parse_sunell_values(text)
        lowered = {key.lower(): value for key, value in values.items()}
        for key in ("ntpip", "ntpserver", "server"):
            if lowered.get(key):
                result["ntp_server"] = lowered[key]
                break
        for key in ("enableflag", "ntpenable", "enable"):
            if key in lowered:
                result["ntp_enabled"] = lowered[key]
                break
        for key in ("timezone", "time_zone", "tz"):
            if lowered.get(key):
                result["timezone"] = lowered[key]
                break
    if authenticated:
        result["source"] = "sunell"
    result["message"] = "; ".join(messages)[-1000:]
    return result


def audit_onvif(ip: str, username: str, password: str, path: str, timeout: float) -> dict:
    result = {
        "source": "",
        "ntp_enabled": "",
        "ntp_server": "",
        "timezone": "",
        "daylight_savings": "",
        "utc_time": "",
        "message": "",
    }
    root = onvif_post(
        ip,
        path,
        username,
        password,
        timeout,
        "GetSystemDateAndTime",
        "<tds:GetSystemDateAndTime/>",
    )
    fault = compact_fault(root)
    if fault:
        raise RuntimeError(fault)
    time_data = parse_system_time(root)
    result.update(time_data)
    result["daylight_savings"] = result.pop("daylight", "")
    result["source"] = "onvif"
    try:
        ntp_root = onvif_post(ip, path, username, password, timeout, "GetNTP", "<tds:GetNTP/>")
        ntp_fault = compact_fault(ntp_root)
        if ntp_fault:
            result["message"] = "GetNTP: " + ntp_fault
        else:
            result["ntp_server"] = parse_ntp(ntp_root)
            result["ntp_enabled"] = "configured" if result["ntp_server"] else "not_configured"
    except NETWORK_ERRORS as exc:
        result["message"] = f"GetNTP: {exc}"
    return result


def audit_one(inventory_row: dict, credentials: list[tuple[str, str]], rules, args) -> dict:
    ip = inventory_row["current_ip"].strip()
    row = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "ip": ip,
        "model": inventory_row.get("model", ""),
        "mac": inventory_row.get("mac", ""),
        "discovery_protocol": inventory_row.get("protocol", ""),
        "status": "failed",
        "source": "",
        "username": "",
        "ntp_enabled": "",
        "ntp_server": "",
        "timezone": "",
        "daylight_savings": "",
        "utc_time": "",
        "message": "",
    }
    if not args.skip_ping and not ping(ip, args.ping_timeout_ms):
        row["message"] = "no ping reply"
        return row

    errors = []
    protocol = inventory_row.get("protocol", "").lower()
    for username, password in credentials_for(ip, credentials, rules):
        methods = ("sunell", "onvif") if protocol == "sunell" else ("onvif", "sunell")
        authenticated = False
        sources = []
        messages = []
        for method in methods:
            try:
                if method == "onvif":
                    values = audit_onvif(ip, username, password, args.onvif_path, args.http_timeout)
                else:
                    values = audit_sunell(ip, username, password, args.http_timeout)
                    if not values["source"]:
                        raise RuntimeError(values["message"] or "Sunell time API unavailable")
                authenticated = True
                sources.append(values.pop("source"))
                if values.get("message"):
                    messages.append(values.pop("message"))
                for key, value in values.items():
                    if value and not row.get(key):
                        row[key] = value
                if row["ntp_server"] and row["timezone"]:
                    break
            except (NETWORK_ERRORS, RuntimeError, ValueError) as exc:
                errors.append(f"{method}/{username}: {exc}")
        if authenticated:
            row["source"] = "+".join(dict.fromkeys(source for source in sources if source))
            row["username"] = username
            row["message"] = "; ".join(messages)[-1000:]
            row["status"] = "ok" if row["ntp_server"] and row["timezone"] else "partial"
            return row
    row["message"] = "; ".join(errors)[-1500:]
    return row


def audit_inventory(args) -> int:
    input_path = pathlib.Path(args.input)
    with input_path.open("r", newline="", encoding="utf-8-sig") as file:
        inventory = [row for row in csv.DictReader(file) if row.get("current_ip", "").strip()]
    if not inventory:
        raise SystemExit(f"No cameras in inventory: {input_path}")

    credentials = list(args.credential)
    if not credentials:
        password = args.password
        if password is None:
            password = getpass.getpass(f"Password for {args.username}: ")
        credentials = [(args.username, password)]
    try:
        rules = [parse_credential_rule(value) for value in args.credential_for]
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(audit_one, item, credentials, rules, args) for item in inventory]
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            rows.append(row)
            print(
                f"{row['ip']}: {row['status'].upper()} "
                f"NTP={row['ntp_server'] or '-'} TZ={row['timezone'] or '-'} via {row['source'] or '-'}"
            )

    rows.sort(key=lambda row: int(ipaddress.IPv4Address(row["ip"])))
    output_path = pathlib.Path(args.output)
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    counts = {status: sum(1 for row in rows if row["status"] == status) for status in ("ok", "partial", "failed")}
    print(f"Written: {output_path}")
    print(f"Results: OK={counts['ok']}, partial={counts['partial']}, failed={counts['failed']}")
    return 0
