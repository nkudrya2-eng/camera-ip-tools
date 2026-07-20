import argparse
import concurrent.futures
import csv
import datetime as dt
import getpass
import ipaddress
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


def offset_ip(ip: str, offset: int) -> str:
    return str(ipaddress.IPv4Address(int(ipaddress.IPv4Address(ip)) + offset))


def ping(ip: str, args: argparse.Namespace) -> bool:
    camera_api.flush_arp(ip)
    return camera_api.ping(ip, args.ping_timeout_ms)


def scan_reachable(ips: list[str], args: argparse.Namespace) -> list[str]:
    if args.workers <= 1:
        return [ip for ip in ips if ping(ip, args)]

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_by_ip = {executor.submit(ping, ip, args): ip for ip in ips}
        for future in concurrent.futures.as_completed(future_by_ip):
            ip = future_by_ip[future]
            if future.result():
                found.append(ip)
    return sorted(found, key=lambda value: int(ipaddress.IPv4Address(value)))


def target_is_free(ip: str, args: argparse.Namespace) -> bool:
    for _ in range(args.free_checks):
        if ping(ip, args):
            return False
        time.sleep(args.poll_seconds)
    return True


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


def move_one(source_ip: str, target_ip: str, credentials: list[tuple[str, str]], args: argparse.Namespace) -> dict:
    row = build_row(source_ip, target_ip)
    try:
        if not target_is_free(target_ip, args):
            row["status"] = "target_busy"
            row["details"] = "target IP replied before assignment"
            return row

        print(f"Сдвиг: {source_ip} -> {target_ip}")
        changed, username, api = camera_api.set_ip(source_ip, credentials, args, target_ip)
        row["username"] = username
        row["api"] = api
        if changed:
            row["status"] = "command_accepted"
            row["details"] = "next full scan verifies it"
        else:
            row["status"] = "change_failed"
            row["details"] = "camera API did not accept IP change"
    except Exception as exc:
        row["status"] = "error"
        row["details"] = f"{type(exc).__name__}: {exc}"
    return row


def move_all(pairs: list[tuple[str, str]], credentials: list[tuple[str, str]], args: argparse.Namespace) -> list[dict]:
    if args.workers <= 1:
        return [move_one(source_ip, target_ip, credentials, args) for source_ip, target_ip in pairs]

    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(move_one, source_ip, target_ip, credentials, args)
            for source_ip, target_ip in pairs
        ]
        for future in concurrent.futures.as_completed(futures):
            rows.append(future.result())
    return sorted(rows, key=lambda row: int(ipaddress.IPv4Address(row["source_ip"])))


def write_csv(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def append_summary(path: pathlib.Path, old_alive: list[str], new_alive: list[str]) -> None:
    summary_path = path.with_name(path.stem + "_summary.txt")
    with summary_path.open("w", encoding="utf-8") as file:
        file.write("OLD_RANGE_ALIVE_AFTER_SHIFT\n")
        file.write("\n".join(old_alive) + "\n")
        file.write("\nNEW_RANGE_ALIVE_AFTER_SHIFT\n")
        file.write("\n".join(new_alive) + "\n")


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Shift visible cameras by an IP offset to expose duplicate IPs.")
    parser.add_argument("--source-range", type=parse_ip_range, default=parse_ip_range("10.53.240.30-55"))
    parser.add_argument("--offset", type=int, default=100)
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
    parser.add_argument("--free-checks", type=int, default=3)
    parser.add_argument("--cooldown-seconds", type=float, default=15.0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--output-csv", default=str(script_dir / "shift_camera_range.csv"))
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

    target_ips = [offset_ip(ip, args.offset) for ip in args.source_range]
    source_alive = scan_reachable(args.source_range, args)
    pairs = [(ip, offset_ip(ip, args.offset)) for ip in source_alive]

    print(f"Исходный диапазон: {args.source_range[0]} - {args.source_range[-1]}")
    print(f"Новый диапазон: {target_ips[0]} - {target_ips[-1]}")
    print(f"Найдено исходных IP: {len(source_alive)}")

    busy_targets = [ip for ip in target_ips if not target_is_free(ip, args)]
    if busy_targets:
        print("СТОП: целевые IP заняты: " + ", ".join(busy_targets))
        return 1

    if not pairs:
        print("Нет камер в исходном диапазоне.")
        return 0

    rows = move_all(pairs, credentials, args)
    output_path = pathlib.Path(args.output_csv)
    write_csv(output_path, rows)

    accepted = [row for row in rows if row["status"] == "command_accepted"]
    failed = [row for row in rows if row["status"] != "command_accepted"]
    print(f"Команд принято: {len(accepted)}, ошибок: {len(failed)}")

    print(f"Жду {args.cooldown_seconds:g} сек., потом сканирую старый и новый диапазоны.")
    time.sleep(args.cooldown_seconds)

    old_alive = scan_reachable(args.source_range, args)
    new_alive = scan_reachable(target_ips, args)
    append_summary(output_path, old_alive, new_alive)

    print("После сдвига старый диапазон отвечает: " + (", ".join(old_alive) if old_alive else "нет"))
    print("После сдвига новый диапазон отвечает: " + (", ".join(new_alive) if new_alive else "нет"))
    if old_alive:
        print("КАНДИДАТЫ НА ДУБЛЬ: " + ", ".join(old_alive))
    print(f"Лог: {output_path}")
    print(f"Сводка: {output_path.with_name(output_path.stem + '_summary.txt')}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
