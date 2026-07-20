import argparse
import contextlib
import csv
import datetime as dt
import getpass
import ipaddress
import os
import pathlib
import time

import check_duplicate_ips as camera_api


def parse_ip_range(value: str) -> list[str]:
    value = value.strip()
    if "-" not in value:
        ipaddress.IPv4Address(value)
        return [value]

    start_text, end_text = value.split("-", 1)
    start_text = start_text.strip()
    end_text = end_text.strip()
    start = ipaddress.IPv4Address(start_text)
    if "." not in end_text:
        end_text = ".".join(start_text.split(".")[:3] + [end_text])
    end = ipaddress.IPv4Address(end_text)
    if int(end) < int(start):
        raise argparse.ArgumentTypeError(f"Bad IP range: {value}")
    return [str(ipaddress.IPv4Address(ip)) for ip in range(int(start), int(end) + 1)]


def ping(ip: str, args: argparse.Namespace) -> bool:
    camera_api.flush_arp(ip)
    return camera_api.ping(ip, args.ping_timeout_ms)


def find_reachable(scan_ips: list[str], args: argparse.Namespace) -> list[str]:
    found = []
    for ip in scan_ips:
        if ping(ip, args):
            found.append(ip)
    return found


def wait_for_single_camera(scan_ips: list[str], args: argparse.Namespace) -> str:
    while True:
        found = find_reachable(scan_ips, args)
        if len(found) == 1:
            return found[0]
        if not found:
            print("Камер не видно. Подключите одну камеру.")
        else:
            print("В сети больше одной камеры: " + ", ".join(found))
            print("Отключите лишние, IP не меняю.")
        time.sleep(args.scan_pause_seconds)


def wait_for_ping(ip: str, args: argparse.Namespace) -> bool:
    deadline = time.monotonic() + args.verify_timeout
    while time.monotonic() <= deadline:
        if ping(ip, args):
            return True
        time.sleep(args.poll_seconds)
    return False


def wait_until_not_reachable(ip: str, args: argparse.Namespace) -> bool:
    misses = 0
    deadline = time.monotonic() + args.verify_timeout
    while time.monotonic() <= deadline:
        if ping(ip, args):
            misses = 0
        else:
            misses += 1
            if misses >= args.missing_pings:
                return True
        time.sleep(args.poll_seconds)
    return False


def set_ip_quiet(ip: str, credentials: list[tuple[str, str]], args: argparse.Namespace, new_ip: str) -> tuple[bool, str, str]:
    if args.verbose:
        return camera_api.set_ip(ip, credentials, args, new_ip)
    with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stdout(devnull):
        return camera_api.set_ip(ip, credentials, args, new_ip)


def append_log(args: argparse.Namespace, row: dict) -> None:
    path = pathlib.Path(args.output_csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(row))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def build_row(source_ip: str, target_ip: str) -> dict:
    return {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "source_ip": source_ip,
        "target_ip": target_ip,
        "status": "",
        "username": "",
        "api": "",
        "details": "",
    }


def target_is_free(target_ip: str, args: argparse.Namespace) -> bool:
    for _ in range(args.free_checks):
        if ping(target_ip, args):
            return False
        time.sleep(args.poll_seconds)
    return True


def assign_one(source_ip: str, target_ip: str, credentials: list[tuple[str, str]], args: argparse.Namespace) -> dict:
    row = build_row(source_ip, target_ip)

    if source_ip == target_ip:
        row["status"] = "already_target"
        row["details"] = "camera is already on target IP"
        return row

    if not target_is_free(target_ip, args):
        row["status"] = "target_busy"
        row["details"] = "target IP replies before assignment"
        return row

    print(f"{source_ip} -> {target_ip}")
    changed, username, api = set_ip_quiet(source_ip, credentials, args, target_ip)
    row["username"] = username
    row["api"] = api
    if not changed:
        row["status"] = "change_failed"
        row["details"] = "camera API did not accept IP change"
        return row

    wait_until_not_reachable(source_ip, args)
    if wait_for_ping(target_ip, args):
        row["status"] = "ok"
    else:
        row["status"] = "not_verified"
        row["details"] = "target IP did not reply after assignment"
    return row


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Assign cameras one by one during field work.")
    parser.add_argument("--scan-range", type=parse_ip_range, default=parse_ip_range("10.53.240.30-240"))
    parser.add_argument("--target-range", type=parse_ip_range, default=parse_ip_range("10.53.240.145-149"))
    parser.add_argument("--netmask", default="255.255.252.0")
    parser.add_argument("--gateway", default="10.53.240.1")
    parser.add_argument("--primary-dns", default="10.70.200.5")
    parser.add_argument("--secondary-dns", default="10.70.200.6")
    parser.add_argument("--credential", action="append", type=camera_api.parse_credential, default=None)
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--api", choices=("auto", "unv", "sunell"), default="auto")
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--scan-pause-seconds", type=float, default=5.0)
    parser.add_argument("--verify-timeout", type=float, default=60.0)
    parser.add_argument("--missing-pings", type=int, default=3)
    parser.add_argument("--free-checks", type=int, default=3)
    parser.add_argument("--output-csv", default=str(script_dir / "field_assign_cameras.csv"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    credentials = args.credential
    if not credentials:
        password = args.password
        if password is None:
            password = getpass.getpass(f"Password for {args.username}: ")
        credentials = [(args.username, password)]

    print("Режим: подключена только одна камера за раз.")
    print("Сканирую: " + f"{args.scan_range[0]} - {args.scan_range[-1]}")
    print("Назначаю: " + ", ".join(args.target_range))
    print("Остановить: Ctrl+C")

    for target_ip in args.target_range:
        print("")
        print(f"Следующий адрес: {target_ip}")
        source_ip = wait_for_single_camera(args.scan_range, args)
        row = assign_one(source_ip, target_ip, credentials, args)
        append_log(args, row)

        if row["status"] == "ok":
            print(f"OK: {source_ip} -> {target_ip}")
            print("Отключите эту камеру и подключите следующую.")
        elif row["status"] == "already_target":
            print(f"OK: камера уже на {target_ip}")
            print("Отключите эту камеру и подключите следующую.")
        else:
            print(f"ПРОВЕРЬ: {source_ip} -> {target_ip}: {row['status']} {row['details']}")
            print("Остановился, чтобы не продолжать с ошибкой.")
            print(f"Лог: {args.output_csv}")
            return 1

    print("")
    print("Пул назначенных адресов закончился.")
    print(f"Лог: {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
