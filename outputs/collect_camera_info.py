import argparse
import csv
import datetime as dt
import getpass
import http.client
import ipaddress
import json
import pathlib
import subprocess
import urllib.parse
import urllib.error
import urllib.request


ENDPOINTS = {
    "device_info": "/LAPI/V1.0/System/DeviceInfo",
    "network_interfaces": "/LAPI/V1.0/Network/Interfaces",
}

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


def parse_ip(value: str) -> int:
    return int(ipaddress.IPv4Address(value))


def iter_range(start_ip: str, end_ip: str):
    start = parse_ip(start_ip)
    end = parse_ip(end_ip)
    if end < start:
        raise SystemExit("--end-ip must be greater than or equal to --start-ip.")
    for ip_int in range(start, end + 1):
        yield str(ipaddress.IPv4Address(ip_int))


def iter_input_csv(path: pathlib.Path):
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        for row in reader:
            ip = (row.get("assigned_ip") or "").strip()
            if ip:
                yield ip


def build_digest_opener(url: str, username: str, password: str):
    password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, url, username, password)
    auth_handler = urllib.request.HTTPDigestAuthHandler(password_manager)
    return urllib.request.build_opener(auth_handler)


def http_json_get(ip: str, username: str, password: str, timeout: float, path: str):
    url = f"http://{ip}{path}"
    opener = build_digest_opener(url, username, password)
    request = urllib.request.Request(url, method="GET")

    with opener.open(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}


def parse_key_value_text(text: str) -> dict:
    result = {"raw": text}
    for line in text.replace(";", "\n").replace("&", "\n").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().strip("\"'")
        value = value.strip().strip("\"'")
        if key:
            result[key] = value
    return result


def sunell_cgi_get(ip: str, username: str, password: str, timeout: float, info_type: str):
    params = {
        "userName": username,
        "password": password,
        "action": "get",
        "type": info_type,
    }
    if info_type == "localNetwork":
        params["IPProtoVer"] = "1"
        params["netCardId"] = "1"
    url = f"http://{ip}/cgi-bin/param.cgi?{urllib.parse.urlencode(params)}"

    with urllib.request.urlopen(url, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return parse_key_value_text(text)


def find_first(data, names):
    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() in names and value not in ("", None, "null"):
                return value
        for value in data.values():
            found = find_first(value, names)
            if found not in ("", None):
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_first(item, names)
            if found not in ("", None):
                return found
    return ""


def compact_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def collect_one(ip: str, args: argparse.Namespace, password: str) -> dict:
    info = {}
    if args.api in ("auto", "unv"):
        for name, path in ENDPOINTS.items():
            try:
                info[name] = http_json_get(ip, args.username, password, args.http_timeout, path)
                print(f"{ip}: read {path}")
            except NETWORK_ERRORS as exc:
                info[name] = {"error": str(exc)}
                print(f"{ip}: could not read {path}: {exc}")

    lapi_failed = any("error" in value for value in info.values() if isinstance(value, dict))
    if args.api == "sunell" or (args.api == "auto" and lapi_failed):
        info = {}
        for name, info_type in {"device_info": "deviceInfo", "network_interfaces": "localNetwork"}.items():
            try:
                info[name] = sunell_cgi_get(ip, args.username, password, args.http_timeout, info_type)
                print(f"{ip}: read Sunell {info_type}")
            except NETWORK_ERRORS as exc:
                info[name] = {"error": str(exc)}
                print(f"{ip}: could not read Sunell {info_type}: {exc}")

    device_info = info.get("device_info", {})
    network_info = info.get("network_interfaces", {})
    return {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "ip": ip,
        "model": find_first(device_info, {"model", "devicemodel", "modelname", "productmodel", "devicetype"}),
        "serial_number": find_first(
            device_info,
            {
                "sn",
                "serial",
                "serialno",
                "serialnum",
                "serialnumber",
                "devicesn",
                "deviceserialno",
                "serialid",
            },
        ),
        "mac": find_first(network_info, {"mac", "macaddress", "physicaladdress", "hwaddr"}),
        "firmware": find_first(
            device_info,
            {"firmware", "firmwareversion", "softwareversion", "swversion", "version"},
        ),
        "device_info_json": compact_json(device_info),
        "network_json": compact_json(network_info),
    }


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Collect model/SN/MAC/video settings from cameras.")
    parser.add_argument("--start-ip", default=None)
    parser.add_argument("--end-ip", default=None)
    parser.add_argument("--input-csv", default=None, help="CSV with assigned_ip column.")
    parser.add_argument("--output-csv", default=str(script_dir / "camera_info_after.csv"))
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--skip-ping", action="store_true")
    parser.add_argument(
        "--api",
        choices=("auto", "unv", "sunell"),
        default="auto",
        help="Camera API to use. Default: auto.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.input_csv:
        ips = list(dict.fromkeys(iter_input_csv(pathlib.Path(args.input_csv))))
    elif args.start_ip and args.end_ip:
        ips = list(iter_range(args.start_ip, args.end_ip))
    else:
        raise SystemExit("Use either --input-csv or both --start-ip and --end-ip.")

    password = args.password
    if password is None:
        password = getpass.getpass(f"Password for {args.username}: ")

    rows = []
    for ip in ips:
        if not args.skip_ping and not ping(ip, args.ping_timeout_ms):
            print(f"{ip}: no ping reply, skipped")
            continue
        rows.append(collect_one(ip, args, password))

    output_path = pathlib.Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp",
        "ip",
        "model",
        "serial_number",
        "mac",
        "firmware",
        "device_info_json",
        "network_json",
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Written {len(rows)} rows to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
