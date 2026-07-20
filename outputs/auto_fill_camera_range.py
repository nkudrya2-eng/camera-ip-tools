import argparse
import concurrent.futures
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


def parse_ip_list(value: str) -> list[str]:
    ips = []
    for item in value.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        ipaddress.IPv4Address(item)
        ips.append(item)
    if not ips:
        raise argparse.ArgumentTypeError("IP list is empty.")
    return ips


def ping(ip: str, args: argparse.Namespace) -> bool:
    camera_api.flush_arp(ip)
    return camera_api.ping(ip, args.ping_timeout_ms)


def scan_reachable(ips: list[str], args: argparse.Namespace) -> list[str]:
    if args.workers <= 1:
        return [ip for ip in ips if ping(ip, args)]

    reachable = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_by_ip = {executor.submit(ping, ip, args): ip for ip in ips}
        for future in concurrent.futures.as_completed(future_by_ip):
            ip = future_by_ip[future]
            if future.result():
                reachable.append(ip)
    return sorted(reachable, key=lambda ip: int(ipaddress.IPv4Address(ip)))


def missing_ips(target_ips: list[str], reachable: set[str]) -> list[str]:
    return [ip for ip in target_ips if ip not in reachable]


def ip_is_free(ip: str, args: argparse.Namespace) -> bool:
    for _ in range(args.free_checks):
        if ping(ip, args):
            return False
        time.sleep(args.poll_seconds)
    return True


def set_ip_quiet(ip: str, credentials: list[tuple[str, str]], args: argparse.Namespace, new_ip: str) -> tuple[bool, str, str]:
    if args.verbose or args.workers > 1:
        return camera_api.set_ip(ip, credentials, args, new_ip)
    with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stdout(devnull):
        return camera_api.set_ip(ip, credentials, args, new_ip)


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


def append_log(args: argparse.Namespace, row: dict) -> None:
    path = pathlib.Path(args.output_csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(row))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def append_logs(args: argparse.Namespace, rows: list[dict]) -> None:
    for row in rows:
        append_log(args, row)


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


def move_camera(source_ip: str, target_ip: str, credentials: list[tuple[str, str]], args: argparse.Namespace) -> dict:
    row = build_row(source_ip, target_ip)
    try:
        if not ip_is_free(target_ip, args):
            row["status"] = "target_busy"
            row["details"] = "target IP replied before assignment"
            return row

        print(f"Назначаю: {source_ip} -> {target_ip}")
        changed, username, api = set_ip_quiet(source_ip, credentials, args, target_ip)
        row["username"] = username
        row["api"] = api
        if not changed:
            row["status"] = "change_failed"
            row["details"] = "camera API did not accept IP change"
            return row

        if args.verify_after_change:
            wait_until_not_reachable(source_ip, args)
            if wait_for_ping(target_ip, args):
                row["status"] = "ok"
            else:
                row["status"] = "not_verified"
                row["details"] = "target IP did not reply after assignment"
        else:
            row["status"] = "ok"
            row["details"] = "change command accepted; next full scan verifies it"
    except Exception as exc:
        row["status"] = "error"
        row["details"] = f"{type(exc).__name__}: {exc}"
    return row


def move_batch(pairs: list[tuple[str, str]], credentials: list[tuple[str, str]], args: argparse.Namespace) -> list[dict]:
    worker_count = args.command_workers or args.workers
    if worker_count <= 1 or len(pairs) <= 1:
        return [move_camera(source_ip, target_ip, credentials, args) for source_ip, target_ip in pairs]

    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(move_camera, source_ip, target_ip, credentials, args)
            for source_ip, target_ip in pairs
        ]
        for future in concurrent.futures.as_completed(futures):
            rows.append(future.result())
    return sorted(rows, key=lambda row: int(ipaddress.IPv4Address(row["target_ip"])))


def source_candidates(args: argparse.Namespace) -> list[str]:
    return args.source_ips if args.source_ips else args.source_range


def alive_sources(args: argparse.Namespace, skipped_sources: set[str]) -> list[str]:
    target_set = set(args.target_range)
    return [
        ip for ip in scan_reachable(source_candidates(args), args)
        if ip not in skipped_sources and ip not in target_set
    ]


def run_salvo(args: argparse.Namespace, credentials: list[tuple[str, str]]) -> int:
    target_alive = set(scan_reachable(args.target_range, args))
    free_targets = missing_ips(args.target_range, target_alive)
    if not free_targets:
        print("Дырок в целевом диапазоне нет. Готово.")
        return 0

    sources = alive_sources(args, set())
    if not sources:
        print(f"Свободный адрес есть ({free_targets[0]}), но источников не найдено.")
        return 0

    pairs = list(zip(sources, free_targets))
    print("Матрица:")
    for source_ip, target_ip in pairs:
        print(f"  {source_ip} -> {target_ip}")

    rows = move_batch(pairs, credentials, args)
    append_logs(args, rows)
    failed = [row for row in rows if row["status"] != "ok"]
    for row in rows:
        if row["status"] == "ok":
            print(f"OK: {row['source_ip']} -> {row['target_ip']}")
        else:
            print(f"ПРОВЕРЬ: {row['source_ip']} -> {row['target_ip']}: {row['status']} {row['details']}")

    if any(row["status"] not in {"change_failed", "target_busy"} for row in failed):
        print(f"Лог: {args.output_csv}")
        return 1

    print(f"Жду {args.cooldown_seconds:g} сек., потом контрольный скан.")
    time.sleep(args.cooldown_seconds)
    after_targets = scan_reachable(args.target_range, args)
    after_sources = scan_reachable(source_candidates(args), args)
    print("Целевой диапазон после залпа: " + (", ".join(after_targets) if after_targets else "нет ответов"))
    print("Источники после залпа: " + (", ".join(after_sources) if after_sources else "нет ответов"))
    print(f"Лог: {args.output_csv}")
    return 0 if not failed else 1


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Fill empty camera IPs in the target range from cameras found after it.")
    parser.add_argument("--target-range", type=parse_ip_range, default=parse_ip_range("10.53.240.30-55"))
    parser.add_argument("--source-range", type=parse_ip_range, default=parse_ip_range("10.53.240.56-240"))
    parser.add_argument("--source-ips", type=parse_ip_list, default=None)
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
    parser.add_argument("--verify-timeout", type=float, default=60.0)
    parser.add_argument("--missing-pings", type=int, default=3)
    parser.add_argument("--free-checks", type=int, default=3)
    parser.add_argument("--cooldown-seconds", type=float, default=15.0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--command-workers", type=int, default=0)
    parser.add_argument("--salvo", action="store_true")
    parser.add_argument("--output-csv", default=str(script_dir / "auto_fill_camera_range.csv"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-after-change", action="store_true")
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

    print(f"Целевой диапазон: {args.target_range[0]} - {args.target_range[-1]}")
    if args.source_ips:
        print("Ищу лишние камеры только по списку: " + ", ".join(args.source_ips))
    else:
        print(f"Ищу лишние камеры: {args.source_range[0]} - {args.source_range[-1]}")
    print(f"Потоков: {args.workers}")
    if args.command_workers:
        print(f"Потоков команд: {args.command_workers}")
    print(f"Пауза после смены: {args.cooldown_seconds:g} сек.")

    if args.salvo:
        return run_salvo(args, credentials)

    skipped_sources = set()
    while True:
        target_alive = set(scan_reachable(args.target_range, args))
        free_targets = missing_ips(args.target_range, target_alive)
        if not free_targets:
            print("Дырок в целевом диапазоне нет. Готово.")
            return 0

        source_alive = alive_sources(args, skipped_sources)
        if not source_alive:
            print(f"Свободный адрес есть ({free_targets[0]}), но камер после диапазона не найдено.")
            if skipped_sources:
                print("Пропущены из-за отказа API/пароля: " + ", ".join(sorted(skipped_sources, key=lambda ip: int(ipaddress.IPv4Address(ip)))))
            return 0

        pairs = list(zip(source_alive[: args.workers], free_targets[: args.workers]))
        print("Пакет: " + ", ".join(f"{source}->{target}" for source, target in pairs))
        rows = move_batch(pairs, credentials, args)
        append_logs(args, rows)
        failed = [row for row in rows if row["status"] != "ok"]
        for row in rows:
            if row["status"] == "ok":
                print(f"OK: {row['source_ip']} -> {row['target_ip']}")
            else:
                print(f"ПРОВЕРЬ: {row['source_ip']} -> {row['target_ip']}: {row['status']} {row['details']}")
                if row["status"] == "change_failed":
                    skipped_sources.add(row["source_ip"])
                    print(f"ПРОПУСКАЮ: {row['source_ip']} до конца этого запуска")

        critical_failed = [row for row in failed if row["status"] not in {"change_failed", "target_busy"}]
        if critical_failed:
            print(f"Лог: {args.output_csv}")
            return 1

        if any(row["status"] == "ok" for row in rows):
            print("Жду, потом сканирую заново.")
            time.sleep(args.cooldown_seconds)
        else:
            print("Успешных назначений не было, сразу сканирую заново.")


if __name__ == "__main__":
    raise SystemExit(main())
