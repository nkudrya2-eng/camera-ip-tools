import argparse
import csv
import datetime as dt
import getpass
import http.client
import ipaddress
import json
import pathlib
import subprocess
import urllib.error
import urllib.parse
import urllib.request


NETWORK_ERRORS = (
    urllib.error.URLError,
    urllib.error.HTTPError,
    TimeoutError,
    http.client.RemoteDisconnected,
    ConnectionError,
    OSError,
)


def ping(ip: str, timeout_ms: int) -> bool:
    result = subprocess.run(
        ["ping", "-n", "1", "-w", str(timeout_ms), ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def iter_range(start_ip: str, end_ip: str):
    start = int(ipaddress.IPv4Address(start_ip))
    end = int(ipaddress.IPv4Address(end_ip))
    if end < start:
        raise SystemExit("--end-ip must be greater than or equal to --start-ip.")
    for value in range(start, end + 1):
        yield str(ipaddress.IPv4Address(value))


def iter_target_ips(args: argparse.Namespace):
    if not args.ip_list:
        yield from iter_range(args.start_ip, args.end_ip)
        return

    for raw in args.ip_list.split(","):
        ip = raw.strip()
        if not ip:
            continue
        ipaddress.IPv4Address(ip)
        yield ip


def parse_credential(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("Use username:password, for example Admin:password")
    username, password = value.split(":", 1)
    username = username.strip()
    password = password.strip()
    if not username:
        raise argparse.ArgumentTypeError("Credential username is empty.")
    return username, password


def build_digest_opener(url: str, username: str, password: str):
    password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, url, username, password)
    return urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(password_manager),
        urllib.request.HTTPBasicAuthHandler(password_manager),
    )


def lapi_get(ip: str, username: str, password: str, timeout: float, path: str) -> dict:
    url = f"http://{ip}{path}"
    opener = build_digest_opener(url, username, password)
    request = urllib.request.Request(url, method="GET")
    with opener.open(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        return json.loads(text)


def lapi_put(ip: str, username: str, password: str, timeout: float, path: str, payload: dict) -> tuple[bool, str]:
    url = f"http://{ip}{path}"
    opener = build_digest_opener(url, username, password)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with opener.open(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        return lapi_success(text), text


def lapi_success(text: str) -> bool:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "Succeed" in text
    response = data.get("Response", {})
    return (
        response.get("ResponseCode") == 0
        or response.get("ResponseString") == "Succeed"
        or response.get("StatusString") == "Succeed"
    )


def response_data(response: dict):
    if isinstance(response, dict) and isinstance(response.get("Response"), dict):
        return response["Response"].get("Data", response)
    return response


def patch_ntp_value(data, ntp_server: str, timezone: str, interval: int) -> bool:
    changed = False
    if isinstance(data, dict):
        for key, value in list(data.items()):
            low = key.lower()
            if low in {"enabled", "enable", "ntpenable", "ntpenabled", "enabledntp"}:
                data[key] = 1 if isinstance(value, int) else True
                changed = True
            elif low in {"mode", "synctype", "timesyncmode"} and isinstance(value, (str, int)):
                data[key] = "NTP" if isinstance(value, str) else value
                changed = True
            elif "server" in low and "ntp" in low:
                data[key] = ntp_server
                changed = True
            elif low in {"server", "address", "ipaddress", "host", "hostname"} and isinstance(value, str):
                data[key] = ntp_server
                changed = True
            elif "interval" in low and isinstance(value, int):
                data[key] = interval
                changed = True
            elif "timezone" in low or low in {"tz", "time_zone"}:
                data[key] = timezone
                changed = True
            else:
                changed = patch_ntp_value(value, ntp_server, timezone, interval) or changed
    elif isinstance(data, list):
        for item in data:
            changed = patch_ntp_value(item, ntp_server, timezone, interval) or changed
    return changed


def make_lapi_payload(original: dict, ntp_server: str, timezone: str, interval: int) -> dict:
    payload = json.loads(json.dumps(original))
    data = response_data(payload)
    changed = patch_ntp_value(data, ntp_server, timezone, interval)
    if not changed and isinstance(data, dict):
        data.update(
            {
                "Enabled": True,
                "NTPServer": ntp_server,
                "Interval": interval,
                "TimeZone": timezone,
            }
        )
    return data if isinstance(data, dict) else payload


def sync_lapi(ip: str, username: str, password: str, args: argparse.Namespace) -> tuple[bool, str]:
    current = lapi_get(ip, username, password, args.http_timeout, "/LAPI/V1.0/Network/NTP")
    payload = make_lapi_payload(current, args.ntp_server, args.timezone, args.interval)
    if args.dry_run:
        return True, "dry-run LAPI payload: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ok, text = lapi_put(ip, username, password, args.http_timeout, "/LAPI/V1.0/Network/NTP", payload)
    return ok, text[:500]


def sunell_get(ip: str, username: str, password: str, timeout: float, info_type: str) -> str:
    params = {"userName": username, "password": password, "action": "get", "type": info_type}
    url = f"http://{ip}/cgi-bin/param.cgi?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def sunell_set(ip: str, username: str, password: str, timeout: float, params: dict) -> tuple[bool, str]:
    full_params = {"userName": username, "password": password, "action": "set"}
    full_params.update(params)
    url = f"http://{ip}/cgi-bin/param.cgi?{urllib.parse.urlencode(full_params)}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        return response.status == 200, text[:500]


def sunell_response_ok(text: str) -> bool:
    lower = text.lower()
    return "error" not in lower and "return=-" not in lower


def sunell_probe(ip: str, username: str, password: str, args: argparse.Namespace) -> str:
    types = [
        "time",
        "dateTime",
        "systemTime",
        "timeConfig",
        "timeSetting",
        "ntp",
        "NTP",
        "ntpConfig",
        "ntpServer",
        "localTime",
        "system",
        "deviceInfo",
    ]
    results = []
    for info_type in types:
        try:
            text = sunell_get(ip, username, password, args.http_timeout, info_type)
            status = "OK" if sunell_response_ok(text) else "ERR"
            compact = " ".join(text.strip().split())[:350]
            print(f"{ip}: probe Sunell {info_type}: {status}: {compact}")
            results.append(f"{info_type}={status}:{compact}")
        except NETWORK_ERRORS as exc:
            print(f"{ip}: probe Sunell {info_type}: {exc}")
            results.append(f"{info_type}=EXC:{exc}")
    return " | ".join(results)


def sync_sunell(ip: str, username: str, password: str, args: argparse.Namespace) -> tuple[bool, str]:
    # Sunell variants differ by firmware; try common names and log the first success.
    candidates = [
        {
            "type": "NTP",
            "enableFlag": "1",
            "IPProtoVer": "1",
            "NTPIP": args.ntp_server,
            "NTPPort": "123",
            "NTPCheckTime": str(args.interval * 60),
        },
        {
            "type": "NTP",
            "enableFlag": "1",
            "IPProtoVer": "1",
            "NTPIP": args.ntp_server,
            "NTPPort": "123",
            "NTPCheckTime": str(args.interval),
        },
        {
            "type": "ntp",
            "server": args.ntp_server,
            "enable": "1",
            "interval": str(args.interval),
            "timeZone": args.timezone,
        },
        {
            "type": "time",
            "ntpServer": args.ntp_server,
            "ntpEnable": "1",
            "ntpInterval": str(args.interval),
            "timeZone": args.timezone,
        },
        {
            "type": "dateTime",
            "ntpServer": args.ntp_server,
            "ntpEnable": "1",
            "ntpInterval": str(args.interval),
            "timeZone": args.timezone,
        },
        {
            "type": "systemTime",
            "ntpServer": args.ntp_server,
            "ntpEnable": "1",
            "ntpInterval": str(args.interval),
            "timeZone": args.timezone,
        },
        {
            "type": "timeConfig",
            "ntpServer": args.ntp_server,
            "ntpEnable": "1",
            "ntpInterval": str(args.interval),
            "timeZone": args.timezone,
        },
        {
            "type": "timeSetting",
            "ntpServer": args.ntp_server,
            "ntpEnable": "1",
            "ntpInterval": str(args.interval),
            "timeZone": args.timezone,
        },
        {
            "type": "ntpConfig",
            "server": args.ntp_server,
            "enable": "1",
            "interval": str(args.interval),
            "timeZone": args.timezone,
        },
    ]
    errors = []
    for params in candidates:
        try:
            if args.dry_run:
                return True, "dry-run Sunell params: " + urllib.parse.urlencode({k: v for k, v in params.items()})
            ok, text = sunell_set(ip, username, password, args.http_timeout, params)
            if ok and sunell_response_ok(text):
                return True, f"Sunell {params['type']}: {text}"
            errors.append(f"{params['type']}: {text}")
        except NETWORK_ERRORS as exc:
            errors.append(f"{params['type']}: {exc}")
    return False, "; ".join(errors)


def sync_one(ip: str, credentials: list[tuple[str, str]], args: argparse.Namespace) -> dict:
    row = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "ip": ip,
        "ok": "0",
        "api": "",
        "username": "",
        "message": "",
    }
    if not args.skip_ping and not ping(ip, args.ping_timeout_ms):
        row["message"] = "no ping reply"
        return row

    for username, password in credentials:
        print(f"{ip}: trying as {username}")
        if args.probe_only:
            row.update({"api": "probe", "username": username, "message": sunell_probe(ip, username, password, args)})
            return row
        apis = ("lapi", "sunell") if args.api == "auto" else (args.api,)
        for api in apis:
            try:
                ok, message = sync_lapi(ip, username, password, args) if api == "lapi" else sync_sunell(ip, username, password, args)
                row.update({"api": api, "username": username, "message": message})
                if ok:
                    row["ok"] = "1"
                    print(f"{ip}: time sync OK by {api}")
                    return row
                print(f"{ip}: {api} failed: {message}")
            except NETWORK_ERRORS as exc:
                row.update({"api": api, "username": username, "message": str(exc)})
                print(f"{ip}: {api} failed: {exc}")
            except Exception as exc:
                row.update({"api": api, "username": username, "message": f"{type(exc).__name__}: {exc}"})
                print(f"{ip}: {api} failed: {type(exc).__name__}: {exc}")
    return row


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Enable NTP time synchronization on cameras.")
    parser.add_argument("--start-ip", default="10.53.240.30")
    parser.add_argument("--end-ip", default="10.53.240.132")
    parser.add_argument("--ip-list", default="", help="Comma-separated IP list; overrides --start-ip/--end-ip.")
    parser.add_argument("--ntp-server", default="10.53.240.12")
    parser.add_argument("--timezone", default="+08:00")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--credential", action="append", type=parse_credential, default=None)
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--api", choices=("auto", "lapi", "sunell"), default="auto")
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--skip-ping", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe-only", action="store_true", help="Only read likely Sunell time sections; do not change settings.")
    parser.add_argument("--output-csv", default=str(script_dir / "time_sync_results.csv"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    credentials = args.credential
    if not credentials:
        password = args.password
        if password is None:
            password = getpass.getpass(f"Password for {args.username}: ")
        credentials = [(args.username, password)]

    rows = []
    for ip in iter_target_ips(args):
        rows.append(sync_one(ip, credentials, args))

    output_path = pathlib.Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["timestamp", "ip", "ok", "api", "username", "message"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Written {len(rows)} rows to {output_path}")
    print(f"OK: {sum(1 for row in rows if row['ok'] == '1')}, failed: {sum(1 for row in rows if row['ok'] != '1')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
