import argparse
import csv
import datetime as dt
import getpass
import http.client
import ipaddress
import json
import pathlib
import subprocess
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
    result = subprocess.run(
        ["arp", "-d", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def iter_ips(start_ip: str, end_ip: str):
    start = int(ipaddress.IPv4Address(start_ip))
    end = int(ipaddress.IPv4Address(end_ip))
    if end < start:
        raise SystemExit("--end-ip must be greater than or equal to --start-ip.")
    for value in range(start, end + 1):
        yield str(ipaddress.IPv4Address(value))


def increment_ip(ip: str, offset: int) -> str:
    return str(ipaddress.IPv4Address(int(ipaddress.IPv4Address(ip)) + offset))


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
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(password_manager))


def response_succeeded(response_text: str) -> bool:
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return "Succeed" in response_text
    response = data.get("Response", {})
    return (
        response.get("ResponseCode") == 0
        or response.get("ResponseString") == "Succeed"
        or response.get("StatusString") == "Succeed"
    )


def sunell_response_succeeded(response_text: str) -> bool:
    lower = response_text.lower()
    return "error" not in lower and "return=-" not in lower


def build_unv_payload(new_ip: str, netmask: str, gateway: str) -> dict:
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
                    "AddressList": [{"Address": new_ip, "Netmask": netmask, "Gateway": gateway}],
                },
                "IPv6": {
                    "IPGetType": 1,
                    "AddressNum": 1,
                    "AddressList": [{"PrefixLenth": 64, "Address": "", "Gateway": ""}],
                },
            }
        ],
        "DefaultRouteNIC": 1,
        "WorkMode": 0,
    }


def set_ip_unv(ip: str, username: str, password: str, args: argparse.Namespace, new_ip: str) -> bool:
    url = f"http://{ip}/LAPI/V1.0/Network/Interfaces"
    opener = build_digest_opener(url, username, password)
    request = urllib.request.Request(
        url,
        data=json.dumps(build_unv_payload(new_ip, args.netmask, args.gateway)).encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with opener.open(request, timeout=args.http_timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        print(f"{ip}: UNV HTTP {response.status}: {text.strip()}")
        return response.status == 200 and response_succeeded(text)


def set_ip_sunell(ip: str, username: str, password: str, args: argparse.Namespace, new_ip: str) -> bool:
    params = {
        "userName": username,
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
    url = f"http://{ip}/cgi-bin/param.cgi?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=args.http_timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        print(f"{ip}: Sunell HTTP {response.status}: {text.strip()}")
        if response.status != 200 or not sunell_response_succeeded(text):
            return False

    restart_params = {
        "userName": username,
        "password": password,
        "action": "restart",
    }
    restart_url = f"http://{ip}/cgi-bin/operate.cgi?{urllib.parse.urlencode(restart_params)}"
    try:
        with urllib.request.urlopen(restart_url, timeout=args.http_timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            print(f"{ip}: Sunell restart HTTP {response.status}: {text.strip()}")
    except NETWORK_ERRORS as exc:
        print(f"{ip}: restart request did not complete after IP change: {exc}")
    return True


def set_ip(ip: str, credentials: list[tuple[str, str]], args: argparse.Namespace, new_ip: str) -> tuple[bool, str, str]:
    for username, password in credentials:
        print(f"{ip}: trying {args.api} as {username}, set IP {new_ip}")
        apis = ("unv", "sunell") if args.api == "auto" else (args.api,)
        for api in apis:
            try:
                if args.dry_run:
                    print(f"DRY RUN: would set {ip} -> {new_ip} by {api} as {username}")
                    return True, username, api
                ok = set_ip_sunell(ip, username, password, args, new_ip) if api == "sunell" else set_ip_unv(ip, username, password, args, new_ip)
                if ok:
                    return True, username, api
            except NETWORK_ERRORS as exc:
                print(f"{ip}: {api} failed as {username}: {exc}")
    return False, "", ""


def wait_for_ping(ip: str, args: argparse.Namespace, label: str) -> bool:
    started = time.monotonic()
    while time.monotonic() - started <= args.verify_timeout:
        flush_arp(ip)
        if ping(ip, args.ping_timeout_ms):
            print(f"{label} {ip}: replies")
            return True
        time.sleep(args.poll_seconds)
    print(f"{label} {ip}: no reply after {args.verify_timeout:g}s")
    return False


def wait_until_free(ip: str, args: argparse.Namespace) -> bool:
    started = time.monotonic()
    misses = 0
    while time.monotonic() - started <= args.verify_timeout:
        flush_arp(ip)
        if ping(ip, args.ping_timeout_ms):
            misses = 0
        else:
            misses += 1
            if misses >= args.missing_pings:
                print(f"{ip}: looks free")
                return True
        time.sleep(args.poll_seconds)
    print(f"{ip}: still replies")
    return False


def find_free_temp_ip(args: argparse.Namespace, temp_offset: int) -> tuple[str, int]:
    skipped = 0
    while True:
        temp_ip = increment_ip(args.temp_start_ip, temp_offset + skipped)
        flush_arp(temp_ip)
        if not ping(temp_ip, args.ping_timeout_ms):
            print(f"Temporary IP {temp_ip} is free.")
            return temp_ip, skipped
        print(f"Temporary IP {temp_ip} is busy, trying next.")
        skipped += 1
        if skipped > args.max_temp_skips:
            raise SystemExit("Too many busy temporary IPs. Increase --max-temp-skips or change --temp-start-ip.")


def append_log(args: argparse.Namespace, row: dict) -> None:
    path = pathlib.Path(args.output_csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(row))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def check_one(source_ip: str, credentials: list[tuple[str, str]], args: argparse.Namespace, temp_offset: int) -> tuple[dict, int]:
    row = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "source_ip": source_ip,
        "temp_ip": "",
        "status": "",
        "duplicate_detected": "no",
        "busy_temp_skipped": 0,
        "username": "",
        "api": "",
        "details": "",
    }

    flush_arp(source_ip)
    if not ping(source_ip, args.ping_timeout_ms):
        print(f"{source_ip}: no camera reply")
        row["status"] = "no_reply"
        row["details"] = "source IP does not reply"
        return row, temp_offset

    temp_ip, skipped = find_free_temp_ip(args, temp_offset)
    temp_offset += skipped
    row["temp_ip"] = temp_ip
    row["busy_temp_skipped"] = skipped

    ok, username, api = set_ip(source_ip, credentials, args, temp_ip)
    row["username"] = username
    row["api"] = api
    if not ok:
        row["status"] = "move_failed"
        row["details"] = "could not move camera to temporary IP"
        return row, temp_offset

    flush_arp(source_ip)
    flush_arp(temp_ip)
    temp_replies = wait_for_ping(temp_ip, args, "Temporary IP")
    source_is_free = wait_until_free(source_ip, args)

    if not source_is_free:
        row["duplicate_detected"] = "yes"
        row["details"] = "source IP still replies after camera was moved"

    if temp_replies:
        restored, restore_user, restore_api = set_ip(temp_ip, credentials, args, source_ip)
        row["username"] = row["username"] or restore_user
        row["api"] = row["api"] or restore_api
        flush_arp(source_ip)
        flush_arp(temp_ip)
        if restored and wait_for_ping(source_ip, args, "Restored source IP"):
            row["status"] = "duplicate" if row["duplicate_detected"] == "yes" else "ok"
        else:
            row["status"] = "restore_failed"
            row["details"] = (row["details"] + "; " if row["details"] else "") + "could not restore source IP"
    else:
        row["status"] = "temp_not_verified"
        row["details"] = (row["details"] + "; " if row["details"] else "") + "temporary IP did not reply"

    return row, temp_offset + 1


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Temporarily move cameras to detect duplicate IP addresses.")
    parser.add_argument("--start-ip", default="10.53.240.30")
    parser.add_argument("--end-ip", default="10.53.240.132")
    parser.add_argument("--temp-start-ip", default="10.53.240.140")
    parser.add_argument("--netmask", default="255.255.252.0")
    parser.add_argument("--gateway", default="10.53.240.1")
    parser.add_argument("--primary-dns", default="10.70.200.5")
    parser.add_argument("--secondary-dns", default="10.70.200.6")
    parser.add_argument("--credential", action="append", type=parse_credential, default=None)
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--api", choices=("auto", "unv", "sunell"), default="auto")
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--verify-timeout", type=float, default=30.0)
    parser.add_argument("--missing-pings", type=int, default=3)
    parser.add_argument("--max-temp-skips", type=int, default=80)
    parser.add_argument("--output-csv", default=str(script_dir / "duplicate_ip_check.csv"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    credentials = args.credential
    if not credentials:
        password = args.password
        if password is None:
            password = getpass.getpass(f"Password for {args.username}: ")
        credentials = [(args.username, password)]
    print("Credentials to try: " + ", ".join(username for username, _ in credentials))

    duplicate_count = 0
    temp_offset = 0
    output_path = pathlib.Path(args.output_csv)
    if output_path.exists():
        output_path.unlink()

    for source_ip in iter_ips(args.start_ip, args.end_ip):
        print("=" * 60)
        print(f"Checking {source_ip}")
        row, temp_offset = check_one(source_ip, credentials, args, temp_offset)
        if row["duplicate_detected"] == "yes":
            duplicate_count += 1
        append_log(args, row)
        print(f"{source_ip}: {row['status']} duplicate={row['duplicate_detected']}")

    print("=" * 60)
    print(f"Done. Duplicates found: {duplicate_count}. Log: {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
