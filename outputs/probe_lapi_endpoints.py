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
import urllib.request


DEFAULT_ENDPOINTS = [
    "/LAPI/V1.0/System/DeviceInfo",
    "/LAPI/V1.0/System/Time",
    "/LAPI/V1.0/System/Status",
    "/LAPI/V1.0/System/Capabilities",
    "/LAPI/V1.0/Network/Interfaces",
    "/LAPI/V1.0/Network/DNS",
    "/LAPI/V1.0/Network/Ports",
    "/LAPI/V1.0/Network/NTP",
    "/LAPI/V1.0/Network/Discovery",
    "/LAPI/V1.0/Channels",
    "/LAPI/V1.0/Channel",
    "/LAPI/V1.0/Media/Video/Streams",
    "/LAPI/V1.0/Media/Video/Source",
    "/LAPI/V1.0/Media/Audio/Streams",
    "/LAPI/V1.0/Video/Streams",
    "/LAPI/V1.0/Video/Source",
    "/LAPI/V1.0/Image",
    "/LAPI/V1.0/Image/Channels",
    "/LAPI/V1.0/Event/Rules",
    "/LAPI/V1.0/Event/Subscriptions",
    "/LAPI/V1.0/Users",
    "/LAPI/V1.0/Security/Users",
    "/LAPI/V1.0/Storage",
    "/LAPI/V1.0/Storage/Disks",
]

NETWORK_ERRORS = (
    urllib.error.URLError,
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
    for ip_int in range(start, end + 1):
        yield str(ipaddress.IPv4Address(ip_int))


def read_endpoints(path: str | None) -> list[str]:
    if not path:
        return DEFAULT_ENDPOINTS
    items = []
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(line)
    return items


def build_digest_opener(url: str, username: str, password: str):
    password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, url, username, password)
    auth_handler = urllib.request.HTTPDigestAuthHandler(password_manager)
    return urllib.request.build_opener(auth_handler)


def parse_lapi_response(text: str) -> tuple[str, str, str]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return "", "", ""

    response = parsed.get("Response", {})
    code = response.get("ResponseCode", "")
    response_string = response.get("ResponseString", "")
    status_string = response.get("StatusString", "")
    return str(code), str(response_string), str(status_string)


def short_body(raw: bytes, preview_chars: int) -> tuple[str, str, str, str, str]:
    text = raw.decode("utf-8", errors="replace")
    response_code, response_string, status_string = parse_lapi_response(text)
    try:
        parsed = json.loads(text)
        text = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        pass
    return text[:preview_chars], str(len(raw)), response_code, response_string, status_string


def probe_endpoint(ip: str, endpoint: str, args: argparse.Namespace, password: str) -> dict:
    url = f"http://{ip}{endpoint}"
    request = urllib.request.Request(url, method="GET")
    opener = build_digest_opener(url, args.username, password)

    row = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "ip": ip,
        "endpoint": endpoint,
        "ok": "0",
        "status": "",
        "reason": "",
        "response_code": "",
        "response_string": "",
        "status_string": "",
        "bytes": "",
        "body_preview": "",
        "error": "",
    }

    try:
        with opener.open(request, timeout=args.http_timeout) as response:
            body_preview, body_len, response_code, response_string, status_string = short_body(
                response.read(),
                args.preview_chars,
            )
            lapi_ok = response_code in ("", "0") and response_string.lower() != "not supported"
            row.update(
                {
                    "ok": "1" if 200 <= response.status < 300 and lapi_ok else "0",
                    "status": str(response.status),
                    "reason": getattr(response, "reason", ""),
                    "response_code": response_code,
                    "response_string": response_string,
                    "status_string": status_string,
                    "bytes": body_len,
                    "body_preview": body_preview,
                }
            )
    except urllib.error.HTTPError as exc:
        body_preview, body_len, response_code, response_string, status_string = short_body(
            exc.read(),
            args.preview_chars,
        )
        row.update(
            {
                "status": str(exc.code),
                "reason": exc.reason,
                "response_code": response_code,
                "response_string": response_string,
                "status_string": status_string,
                "bytes": body_len,
                "body_preview": body_preview,
            }
        )
    except NETWORK_ERRORS as exc:
        row["error"] = str(exc)

    return row


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Probe which LAPI GET endpoints exist on cameras.")
    parser.add_argument("--ip", default=None)
    parser.add_argument("--start-ip", default=None)
    parser.add_argument("--end-ip", default=None)
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--endpoints-file", default=None)
    parser.add_argument("--output-csv", default=str(script_dir / "lapi_probe.csv"))
    parser.add_argument("--preview-chars", type=int, default=3000)
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--skip-ping", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.ip:
        ips = [args.ip]
    elif args.start_ip and args.end_ip:
        ips = list(iter_range(args.start_ip, args.end_ip))
    else:
        raise SystemExit("Use --ip or both --start-ip and --end-ip.")

    password = args.password
    if password is None:
        password = getpass.getpass(f"Password for {args.username}: ")

    endpoints = read_endpoints(args.endpoints_file)
    rows = []
    for ip in ips:
        if not args.skip_ping and not ping(ip, args.ping_timeout_ms):
            print(f"{ip}: no ping reply, skipped")
            continue
        for endpoint in endpoints:
            row = probe_endpoint(ip, endpoint, args, password)
            rows.append(row)
            if row["ok"] == "1":
                marker = "OK"
            elif row["response_string"]:
                marker = row["response_string"]
            else:
                marker = row["status"] or "ERR"
            print(f"{ip} {endpoint}: {marker}")

    output_path = pathlib.Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp",
        "ip",
        "endpoint",
        "ok",
        "status",
        "reason",
        "response_code",
        "response_string",
        "status_string",
        "bytes",
        "body_preview",
        "error",
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Written {len(rows)} rows to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
