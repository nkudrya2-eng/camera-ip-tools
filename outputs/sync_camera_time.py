import argparse
import csv
import datetime as dt
import getpass
import http.client
import ipaddress
import pathlib
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from probe_onvif_time import (
    compact_fault as onvif_compact_fault,
    onvif_post,
    parse_ntp as onvif_parse_ntp,
    parse_system_time as onvif_parse_system_time,
)
from set_onvif_time import offset_to_onvif_tz, set_ntp_xml, set_system_time_xml


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


def is_account_locked(message: str) -> bool:
    lower = message.lower()
    return "userlocked" in lower or "statuscode\": 364" in lower or "account locked" in lower


def onvif_write(ip: str, username: str, password: str, args: argparse.Namespace, action: str, body_xml: str) -> tuple[str, str]:
    try:
        root = onvif_post(ip, args.onvif_path, username, password, args.http_timeout, action, body_xml)
    except NETWORK_ERRORS as exc:
        return "failed", str(exc)
    except Exception as exc:
        return "failed", f"{type(exc).__name__}: {exc}"

    fault = onvif_compact_fault(root)
    if fault:
        return "failed", fault
    return "ok", "OK"


def onvif_probe_after(ip: str, username: str, password: str, args: argparse.Namespace) -> tuple[str, str, str, str]:
    errors = []
    timezone = ""
    utc_time = ""
    ntp = ""
    try:
        time_root = onvif_post(
            ip,
            args.onvif_path,
            username,
            password,
            args.http_timeout,
            "GetSystemDateAndTime",
            "<tds:GetSystemDateAndTime/>",
        )
        time_data = onvif_parse_system_time(time_root)
        timezone = time_data.get("timezone", "")
        utc_time = time_data.get("utc_time", "")
    except NETWORK_ERRORS as exc:
        errors.append(f"GetSystemDateAndTime: {exc}")

    try:
        ntp_root = onvif_post(ip, args.onvif_path, username, password, args.http_timeout, "GetNTP", "<tds:GetNTP/>")
        ntp = onvif_parse_ntp(ntp_root)
    except NETWORK_ERRORS as exc:
        errors.append(f"GetNTP: {exc}")

    return timezone, utc_time, ntp, "; ".join(errors)


def sync_onvif(ip: str, username: str, password: str, args: argparse.Namespace) -> tuple[bool, str]:
    timezone_onvif = args.timezone_onvif or offset_to_onvif_tz(args.timezone)
    if args.dry_run:
        return True, f"dry-run ONVIF NTP={args.ntp_server}, timezone={timezone_onvif}"

    messages = []
    ntp_write, message = onvif_write(
        ip,
        username,
        password,
        args,
        "SetNTP",
        set_ntp_xml(args.ntp_server, args.from_dhcp),
    )
    if ntp_write != "ok":
        messages.append("SetNTP: " + message)

    timezone_write, message = onvif_write(
        ip,
        username,
        password,
        args,
        "SetSystemDateAndTime",
        set_system_time_xml(timezone_onvif, args.daylight_savings),
    )
    if timezone_write != "ok":
        messages.append("SetSystemDateAndTime: " + message)

    timezone_after, utc_after, ntp_after, verify_errors = onvif_probe_after(ip, username, password, args)
    timezone_ok = timezone_after == timezone_onvif
    ntp_ok = args.ntp_server in ntp_after
    if timezone_ok and ntp_ok:
        return True, f"ONVIF target verified; timezone={timezone_after}; ntp={ntp_after}; utc={utc_after}"

    if verify_errors:
        messages.append("verify: " + verify_errors)
    if not timezone_ok:
        messages.append(f"timezone after is {timezone_after or '<empty>'}, expected {timezone_onvif}")
    if not ntp_ok:
        messages.append(f"NTP after is {ntp_after or '<empty>'}, expected {args.ntp_server}")
    return False, "; ".join(messages) if messages else "ONVIF target not verified"


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
        if args.verbose:
            print(f"{ip}: trying as {username}")
        if args.probe_only:
            row.update({"api": "probe", "username": username, "message": sunell_probe(ip, username, password, args)})
            return row
        apis = ("onvif", "sunell") if args.api == "auto" else (args.api,)
        for api in apis:
            try:
                if api == "onvif":
                    ok, message = sync_onvif(ip, username, password, args)
                else:
                    ok, message = sync_sunell(ip, username, password, args)
                row.update({"api": api, "username": username, "message": message})
                if ok:
                    row["ok"] = "1"
                    print(f"{ip}: time sync OK by {api}")
                    return row
                if args.verbose:
                    print(f"{ip}: {api} failed: {message}")
                if is_account_locked(message):
                    row["message"] = "account locked; stop retries for this IP"
                    print(f"{ip}: account locked; stop retries for this IP")
                    return row
            except NETWORK_ERRORS as exc:
                row.update({"api": api, "username": username, "message": str(exc)})
                if args.verbose:
                    print(f"{ip}: {api} failed: {exc}")
                if is_account_locked(str(exc)):
                    row["message"] = "account locked; stop retries for this IP"
                    print(f"{ip}: account locked; stop retries for this IP")
                    return row
            except Exception as exc:
                row.update({"api": api, "username": username, "message": f"{type(exc).__name__}: {exc}"})
                if args.verbose:
                    print(f"{ip}: {api} failed: {type(exc).__name__}: {exc}")
                if is_account_locked(str(exc)):
                    row["message"] = "account locked; stop retries for this IP"
                    print(f"{ip}: account locked; stop retries for this IP")
                    return row
    print(f"{ip}: FAILED {row['api'] or 'none'} {row['message']}")
    return row


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Enable NTP time synchronization on cameras.")
    parser.add_argument("--start-ip", default="10.53.240.30")
    parser.add_argument("--end-ip", default="10.53.240.132")
    parser.add_argument("--ip-list", default="", help="Comma-separated IP list; overrides --start-ip/--end-ip.")
    parser.add_argument("--ntp-server", default="10.99.200.60")
    parser.add_argument("--timezone", default="+08:00")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--credential", action="append", type=parse_credential, default=None)
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--api", choices=("auto", "onvif", "sunell"), default="auto")
    parser.add_argument("--onvif-path", default="/onvif/device_service")
    parser.add_argument("--timezone-onvif", default="", help="Explicit ONVIF TZ, for example UTC-08:00:00.")
    parser.add_argument("--from-dhcp", action="store_true", help="Use ONVIF NTP from DHCP instead of manual NTP.")
    parser.add_argument("--daylight-savings", action="store_true")
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--skip-ping", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="Print every failed API attempt, not only final result.")
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
