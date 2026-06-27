import argparse
import csv
import datetime as dt
import getpass
import http.client
import ipaddress
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

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


def flush_arp(ip: str) -> bool:
    # On Windows this usually needs an elevated terminal.
    result = subprocess.run(
        ["arp", "-d", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def build_payload(new_ip: str, netmask: str, gateway: str) -> dict:
    return {
        "Num": 1,
        "NetworkInterfaceList": [
            {
                "ID": 1,
                "Name": "eth0",
                "WorkMode": 0,
                "IsInnerNIC": 1,
                "InnerNICIPAddress": new_ip,
                "InnerNICNetmask": netmask,
                "InnerNICName": "eth0",
                "MTU": 1500,
                "MAC": "",
                "NegotiationMode": 0,
                "IPv4": {
                    "IPGetType": 0,
                    "PPPoE": {"LoginName": "", "PIN": ""},
                    "AddressNum": 1,
                    "AddressList": [
                        {
                            "Address": new_ip,
                            "Netmask": netmask,
                            "Gateway": gateway,
                        }
                    ],
                },
                "IPv6": {
                    "IPGetType": 1,
                    "AddressNum": 1,
                    "AddressList": [
                        {
                            "PrefixLenth": 64,
                            "Address": "",
                            "Gateway": "",
                        }
                    ],
                },
            }
        ],
        "DefaultRouteNIC": 1,
        "WorkMode": 0,
    }


def response_succeeded(response_text: str) -> bool:
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return "Succeed" in response_text

    response = data.get("Response", {})
    status = response.get("StatusString", "")
    response_string = response.get("ResponseString", "")
    return status == "Succeed" or response_string == "Succeed"


def lapi_response_succeeded(data) -> bool:
    if not isinstance(data, dict):
        return False
    response = data.get("Response", {})
    return (
        response.get("ResponseCode") == 0
        or response.get("ResponseString") == "Succeed"
        or response.get("StatusString") == "Succeed"
    )


def build_digest_opener(url: str, username: str, password: str):
    password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, url, username, password)
    auth_handler = urllib.request.HTTPDigestAuthHandler(password_manager)
    return urllib.request.build_opener(auth_handler)


def http_json_get(args: argparse.Namespace, password: str, path: str):
    url = f"http://{args.camera_ip}{path}"
    opener = build_digest_opener(url, args.username, password)
    request = urllib.request.Request(url, method="GET")

    with opener.open(request, timeout=args.http_timeout) as response:
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


def sunell_cgi_get(args: argparse.Namespace, password: str, info_type: str) -> dict:
    params = {
        "userName": args.username,
        "password": password,
        "action": "get",
        "type": info_type,
    }
    if info_type == "localNetwork":
        params["IPProtoVer"] = "1"
        params["netCardId"] = "1"
    url = f"http://{args.camera_ip}/cgi-bin/param.cgi?{urllib.parse.urlencode(params)}"

    with urllib.request.urlopen(url, timeout=args.http_timeout) as response:
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
    if data in ("", None):
        return ""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def collect_camera_info(args: argparse.Namespace, password: str) -> dict:
    endpoints = {
        "device_info": "/LAPI/V1.0/System/DeviceInfo",
        "network_interfaces": "/LAPI/V1.0/Network/Interfaces",
    }
    info = {}

    for name, path in endpoints.items():
        try:
            info[name] = http_json_get(args, password, path)
            print(f"Read {path}")
        except NETWORK_ERRORS as exc:
            info[name] = {"error": str(exc)}
            print(f"Could not read {path}: {exc}")

    return info


def collect_sunell_camera_info(args: argparse.Namespace, password: str) -> dict:
    endpoints = {
        "device_info": "deviceInfo",
        "network_interfaces": "localNetwork",
    }
    info = {}

    for name, info_type in endpoints.items():
        try:
            info[name] = sunell_cgi_get(args, password, info_type)
            print(f"Read Sunell {info_type}")
        except NETWORK_ERRORS as exc:
            info[name] = {"error": str(exc)}
            print(f"Could not read Sunell {info_type}: {exc}")

    return info


def network_interface_is_supported(info: dict) -> bool:
    network_info = info.get("network_interfaces", {})
    if lapi_response_succeeded(network_info):
        return True
    response = network_info.get("Response", {}) if isinstance(network_info, dict) else {}
    reason = response.get("ResponseString") or network_info.get("error") or "unknown error"
    print(f"Network interface API is not available: {reason}")
    return False


def append_inventory_log(args: argparse.Namespace, new_ip: str, info: dict) -> None:
    log_path = pathlib.Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    exists = log_path.exists()

    device_info = info.get("device_info", {})
    network_info = info.get("network_interfaces", {})
    row = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "source_ip": args.camera_ip,
        "assigned_ip": new_ip,
        "model": find_first(device_info, {"model", "devicemodel", "modelname"}),
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
                "serialnum",
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

    with log_path.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(row))
        if not exists:
            writer.writeheader()
        writer.writerow(row)
    print(f"Camera info written to {log_path}")


def parse_pool_ip(value: str, *, allow_256: bool = False) -> int:
    parts = value.split(".")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(f"Invalid IP address: {value}")

    try:
        octets = [int(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid IP address: {value}") from exc

    max_last_octet = 256 if allow_256 else 255
    if any(octet < 0 or octet > 255 for octet in octets[:3]):
        raise argparse.ArgumentTypeError(f"Invalid IP address: {value}")
    if octets[3] < 0 or octets[3] > max_last_octet:
        raise argparse.ArgumentTypeError(f"Invalid IP address: {value}")

    if octets[3] == 256:
        octets[3] = 255
    return int(ipaddress.IPv4Address(".".join(str(octet) for octet in octets)))


def iter_target_ips(args: argparse.Namespace):
    if args.start_ip is None and args.end_ip is None:
        yield args.new_ip
        return

    if args.start_ip is None or args.end_ip is None:
        raise SystemExit("Use --start-ip and --end-ip together.")

    start = parse_pool_ip(args.start_ip)
    end = parse_pool_ip(args.end_ip, allow_256=True)
    if end < start:
        raise SystemExit("--end-ip must be greater than or equal to --start-ip.")

    for ip_int in range(start, end + 1):
        yield str(ipaddress.IPv4Address(ip_int))


def target_ip_is_free(args: argparse.Namespace, ip: str) -> bool:
    flush_arp(ip)
    if ping(ip, args.ping_timeout_ms):
        print(f"Skipping {ip}: address is already in use.")
        return False
    print(f"Address {ip} is free.")
    return True


def send_network_command(args: argparse.Namespace, password: str, new_ip: str) -> bool:
    url = f"http://{args.camera_ip}/LAPI/V1.0/Network/Interfaces"
    payload = build_payload(new_ip, args.netmask, args.gateway)
    print(f"Writing IP {new_ip} to camera at {args.camera_ip}")

    if args.dry_run:
        print("DRY RUN: would send:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return True

    opener = build_digest_opener(url, args.username, password)

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={"Content-Type": "application/json"},
    )

    with opener.open(request, timeout=args.http_timeout) as response:
        response_text = response.read().decode("utf-8", errors="replace")
        print(f"HTTP {response.status}: {response_text.strip()}")
        return response.status == 200 and response_succeeded(response_text)


def send_sunell_network_command(args: argparse.Namespace, password: str, new_ip: str) -> bool:
    set_params = {
        "userName": args.username,
        "password": password,
        "action": "set",
        "type": "localNetwork",
        "netCardId": "1",
        "IPProtoVer": "1",
        "IPAddress": new_ip,
        "subNetmask": args.netmask,
        "subGetway": args.gateway,
        "preferredDNS": args.primary_dns,
        "alternateDNS": args.secondary_dns,
    }
    set_url = f"http://{args.camera_ip}/cgi-bin/param.cgi?{urllib.parse.urlencode(set_params)}"
    print(f"Writing IP {new_ip} to Sunell camera at {args.camera_ip}")

    if args.dry_run:
        safe_params = dict(set_params)
        safe_params["password"] = "***"
        print("DRY RUN: would call:")
        print(f"http://{args.camera_ip}/cgi-bin/param.cgi?{urllib.parse.urlencode(safe_params)}")
        return True

    with urllib.request.urlopen(set_url, timeout=args.http_timeout) as response:
        response_text = response.read().decode("utf-8", errors="replace")
        print(f"HTTP {response.status}: {response_text.strip()}")
        if response.status != 200:
            return False

    restart_params = {
        "userName": args.username,
        "password": password,
        "action": "restart",
    }
    restart_url = f"http://{args.camera_ip}/cgi-bin/operate.cgi?{urllib.parse.urlencode(restart_params)}"
    try:
        with urllib.request.urlopen(restart_url, timeout=args.http_timeout) as response:
            response_text = response.read().decode("utf-8", errors="replace")
            print(f"Restart HTTP {response.status}: {response_text.strip()}")
    except NETWORK_ERRORS as exc:
        print(f"Restart request did not complete after IP command: {exc}")
    return True


def send_camera_command(args: argparse.Namespace, password: str, new_ip: str, api: str) -> bool:
    if api == "sunell":
        return send_sunell_network_command(args, password, new_ip)
    return send_network_command(args, password, new_ip)


def wait_until_found(args: argparse.Namespace) -> bool:
    print(f"Waiting for camera at {args.camera_ip}...")
    started_at = time.monotonic()
    while True:
        if ping(args.camera_ip, args.ping_timeout_ms):
            print(f"Found {args.camera_ip}")
            return True
        if args.empty_timeout > 0 and time.monotonic() - started_at >= args.empty_timeout:
            print(f"No camera found for {args.empty_timeout:g} seconds.")
            return False
        time.sleep(args.poll_seconds)


def wait_until_gone(args: argparse.Namespace) -> bool:
    print(f"Waiting until {args.camera_ip} stops replying...")
    started_at = time.monotonic()
    misses = 0
    while misses < args.missing_pings:
        if ping(args.camera_ip, args.ping_timeout_ms):
            misses = 0
        else:
            misses += 1
        if args.gone_timeout > 0 and time.monotonic() - started_at >= args.gone_timeout:
            print(f"{args.camera_ip} still replies after {args.gone_timeout:g} seconds; continuing.")
            return False
        time.sleep(args.poll_seconds)
    print(f"{args.camera_ip} is gone")
    return True


def wait_until_new_ip_found(args: argparse.Namespace, new_ip: str) -> bool:
    print(f"Waiting for camera at new IP {new_ip}...")
    started_at = time.monotonic()
    while True:
        flush_arp(new_ip)
        if ping(new_ip, args.ping_timeout_ms):
            print(f"New IP {new_ip} replies.")
            return True
        if args.verify_timeout > 0 and time.monotonic() - started_at >= args.verify_timeout:
            print(f"New IP {new_ip} did not reply after {args.verify_timeout:g} seconds.")
            return False
        time.sleep(args.poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wait for a camera at one IP, send LAPI network settings, flush ARP, repeat."
    )
    parser.add_argument("--camera-ip", default="192.168.0.250")
    parser.add_argument("--new-ip", default="192.168.0.251")
    parser.add_argument("--start-ip", default=None, help="First IP address from the pool.")
    parser.add_argument("--end-ip", default=None, help="Last IP address from the pool.")
    parser.add_argument("--netmask", default="255.255.255.0")
    parser.add_argument("--gateway", default="192.168.15.1")
    parser.add_argument("--primary-dns", default="128.0.0.1")
    parser.add_argument("--secondary-dns", default="128.0.0.2")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument(
        "--log-file",
        default=str(pathlib.Path(__file__).with_name("camera_inventory.csv")),
    )
    parser.add_argument("--missing-pings", type=int, default=3)
    parser.add_argument(
        "--gone-timeout",
        type=float,
        default=20,
        help="Maximum seconds to wait until the old camera IP stops replying. Default: 20.",
    )
    parser.add_argument(
        "--verify-timeout",
        type=float,
        default=30,
        help="Maximum seconds to wait until the new camera IP replies. Default: 30.",
    )
    parser.add_argument(
        "--no-verify-new-ip",
        action="store_true",
        help="Do not stop if the new IP address does not reply after changing settings.",
    )
    parser.add_argument(
        "--empty-timeout",
        type=float,
        default=0,
        help="Stop if no camera appears for this many seconds. Default: wait forever.",
    )
    parser.add_argument("--once", action="store_true", help="Send once and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print JSON without sending.")
    parser.add_argument(
        "--no-free-check",
        action="store_true",
        help="Do not ping target IP addresses before assigning them.",
    )
    parser.add_argument(
        "--retry-on-error",
        action="store_true",
        help="Keep retrying after a camera returns an error.",
    )
    parser.add_argument(
        "--api",
        choices=("auto", "unv", "sunell"),
        default="auto",
        help="Camera API to use. Default: auto.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    password = args.password
    if password is None and not args.dry_run:
        password = getpass.getpass(f"Password for {args.username}: ")

    for new_ip in iter_target_ips(args):
        if not args.no_free_check and not target_ip_is_free(args, new_ip):
            continue

        if not wait_until_found(args):
            print("No more cameras detected.")
            return 0

        api = args.api
        if not args.dry_run and args.api == "sunell":
            info = collect_sunell_camera_info(args, password or "")
            append_inventory_log(args, new_ip, info)
        elif not args.dry_run:
            info = collect_camera_info(args, password or "")
            if not network_interface_is_supported(info):
                if args.api == "unv":
                    append_inventory_log(args, new_ip, info)
                    print("Command failed; camera does not support the network interface API.")
                    return 1
                print("LAPI is not available; trying Sunell CGI.")
                api = "sunell"
                info = collect_sunell_camera_info(args, password or "")
            append_inventory_log(args, new_ip, info)

        try:
            ok = send_camera_command(args, password or "", new_ip, api)
        except NETWORK_ERRORS as exc:
            print(f"Request failed: {exc}", file=sys.stderr)
            ok = False

        if flush_arp(args.camera_ip):
            print(f"ARP cache entry for {args.camera_ip} cleared")
        else:
            print("Could not clear ARP cache. Run the terminal as Administrator.")
        flush_arp(new_ip)

        if args.once:
            return 0 if ok else 1

        if ok:
            wait_until_gone(args)
            flush_arp(args.camera_ip)
            if not args.no_verify_new_ip and not wait_until_new_ip_found(args, new_ip):
                print("Command succeeded, but the new IP was not verified; stopping.")
                return 1
        elif not args.retry_on_error:
            print("Command failed; stopping to avoid repeated login attempts.")
            return 1
        else:
            print("Command failed; will retry after a short pause.")
            time.sleep(args.poll_seconds)

    print("IP pool is finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
