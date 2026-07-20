import argparse
import contextlib
import csv
import datetime as dt
import getpass
import ipaddress
import os
import pathlib
import queue
import threading
import time

import check_duplicate_ips as dup


def verbose_print(args: argparse.Namespace, message: str) -> None:
    if args.verbose:
        print(message)


def set_ip_quiet(ip: str, credentials: list[tuple[str, str]], args: argparse.Namespace, new_ip: str) -> tuple[bool, str, str]:
    if args.verbose:
        return dup.set_ip(ip, credentials, args, new_ip)
    with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stdout(devnull):
        return dup.set_ip(ip, credentials, args, new_ip)


class RecoveryPool:
    def __init__(self, ips: list[str]):
        self._ips = ips
        self._used = set()
        self._lock = threading.Lock()

    def claim_free(self, args: argparse.Namespace) -> str:
        with self._lock:
            for ip in self._ips:
                if ip in self._used:
                    continue
                dup.flush_arp(ip)
                if not dup.ping(ip, args.ping_timeout_ms):
                    self._used.add(ip)
                    verbose_print(args, f"Recovery IP {ip} is free and reserved.")
                    return ip
            raise RuntimeError("No free recovery IP left.")

    def release(self, ip: str) -> None:
        with self._lock:
            self._used.discard(ip)


class CsvLog:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self._lock = threading.Lock()
        self.fieldnames = [
            "timestamp",
            "worker",
            "source_ip",
            "temp_ip",
            "recovery_ip",
            "status",
            "duplicate_detected",
            "username",
            "api",
            "details",
        ]
        if self.path.exists():
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, row: dict) -> None:
        with self._lock:
            exists = self.path.exists()
            with self.path.open("a", newline="", encoding="utf-8-sig") as file:
                writer = csv.DictWriter(file, fieldnames=self.fieldnames)
                if not exists:
                    writer.writeheader()
                writer.writerow({name: row.get(name, "") for name in self.fieldnames})


def parse_ip_range(value: str) -> list[str]:
    if "-" not in value:
        ipaddress.IPv4Address(value)
        return [value]
    start_text, end_text = value.split("-", 1)
    start_text = start_text.strip()
    end_text = end_text.strip()
    start = ipaddress.IPv4Address(start_text)
    if "." not in end_text:
        parts = start_text.split(".")
        end_text = ".".join(parts[:3] + [end_text])
    end = ipaddress.IPv4Address(end_text)
    if int(end) < int(start):
        raise argparse.ArgumentTypeError(f"Bad IP range: {value}")
    return [str(ipaddress.IPv4Address(value)) for value in range(int(start), int(end) + 1)]


def parse_temp_pools(value: str) -> list[list[str]]:
    pools = []
    for item in value.split(";"):
        item = item.strip()
        if item:
            pools.append(parse_ip_range(item))
    if not pools:
        raise argparse.ArgumentTypeError("At least one temp pool is required.")
    return pools


def iter_scan_ips(args: argparse.Namespace, recovery_ips: set[str]) -> list[str]:
    ips = list(dup.iter_ips(args.start_ip, args.end_ip))
    if args.skip_recovery_pool:
        ips = [ip for ip in ips if ip not in recovery_ips]
    return ips


def find_free_temp_ip(pool: list[str], args: argparse.Namespace) -> str:
    for temp_ip in pool:
        dup.flush_arp(temp_ip)
        if not dup.ping(temp_ip, args.ping_timeout_ms):
            verbose_print(args, f"Temporary IP {temp_ip} is free.")
            return temp_ip
    raise RuntimeError("No free temp IP in this worker pool.")


def wait_for_ping(ip: str, args: argparse.Namespace, label: str) -> bool:
    started = time.monotonic()
    while time.monotonic() - started <= args.verify_timeout:
        dup.flush_arp(ip)
        if dup.ping(ip, args.ping_timeout_ms):
            verbose_print(args, f"{label} {ip}: replies")
            return True
        time.sleep(args.poll_seconds)
    verbose_print(args, f"{label} {ip}: no reply after {args.verify_timeout:g}s")
    return False


def wait_until_free(ip: str, args: argparse.Namespace) -> bool:
    misses = 0
    started = time.monotonic()
    while time.monotonic() - started <= args.verify_timeout:
        dup.flush_arp(ip)
        if dup.ping(ip, args.ping_timeout_ms):
            misses = 0
        else:
            misses += 1
            if misses >= args.missing_pings:
                verbose_print(args, f"{ip}: looks free")
                return True
        time.sleep(args.poll_seconds)
    verbose_print(args, f"{ip}: still replies")
    return False


def base_row(worker_name: str, source_ip: str, temp_ip: str = "") -> dict:
    return {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "worker": worker_name,
        "source_ip": source_ip,
        "temp_ip": temp_ip,
        "recovery_ip": "",
        "status": "",
        "duplicate_detected": "no",
        "username": "",
        "api": "",
        "details": "",
    }


def process_one(
    source_ip: str,
    worker_name: str,
    temp_pool: list[str],
    recovery_pool: RecoveryPool,
    credentials: list[tuple[str, str]],
    args: argparse.Namespace,
) -> dict:
    row = base_row(worker_name, source_ip)
    dup.flush_arp(source_ip)
    if not dup.ping(source_ip, args.ping_timeout_ms):
        row["status"] = "no_reply"
        row["details"] = "source IP does not reply"
        return row

    try:
        temp_ip = find_free_temp_ip(temp_pool, args)
    except RuntimeError as exc:
        row["status"] = "no_temp_ip"
        row["details"] = str(exc)
        return row
    row["temp_ip"] = temp_ip

    moved, username, api = set_ip_quiet(source_ip, credentials, args, temp_ip)
    row["username"] = username
    row["api"] = api
    if not moved:
        row["status"] = "move_to_temp_failed"
        row["details"] = "could not move camera to temporary IP"
        return row

    dup.flush_arp(source_ip)
    dup.flush_arp(temp_ip)
    temp_replies = wait_for_ping(temp_ip, args, "Temporary IP")
    source_is_free = wait_until_free(source_ip, args)
    if not temp_replies:
        row["status"] = "temp_not_verified"
        row["details"] = "temporary IP did not reply"
        return row

    if source_is_free:
        restored, restore_user, restore_api = set_ip_quiet(temp_ip, credentials, args, source_ip)
        row["username"] = row["username"] or restore_user
        row["api"] = row["api"] or restore_api
        if restored and wait_for_ping(source_ip, args, "Restored source IP"):
            row["status"] = "ok_no_duplicate"
        else:
            row["status"] = "restore_failed"
            row["details"] = "no duplicate; could not restore source IP"
        return row

    row["duplicate_detected"] = "yes"
    row["details"] = "source IP still replies after moved camera went to temp"
    try:
        recovery_ip = recovery_pool.claim_free(args)
    except RuntimeError as exc:
        row["status"] = "duplicate_no_recovery_ip"
        row["details"] += f"; {exc}; camera remains at temp IP"
        return row

    row["recovery_ip"] = recovery_ip
    recovered, recovery_user, recovery_api = set_ip_quiet(temp_ip, credentials, args, recovery_ip)
    row["username"] = row["username"] or recovery_user
    row["api"] = row["api"] or recovery_api
    if recovered and wait_for_ping(recovery_ip, args, "Recovery IP"):
        row["status"] = "duplicate_recovered"
        return row

    recovery_pool.release(recovery_ip)
    row["status"] = "recovery_failed"
    row["details"] += "; could not move temp camera to recovery IP"
    return row


def worker(
    worker_index: int,
    temp_pool: list[str],
    work_queue: queue.Queue,
    recovery_pool: RecoveryPool,
    credentials: list[tuple[str, str]],
    args: argparse.Namespace,
    log: CsvLog,
    stop_event: threading.Event,
) -> None:
    worker_name = f"worker-{worker_index + 1}"
    while True:
        if stop_event.is_set():
            return
        try:
            source_ip = work_queue.get_nowait()
        except queue.Empty:
            return
        try:
            verbose_print(args, "=" * 60)
            verbose_print(args, f"{worker_name}: checking {source_ip}")
            row = process_one(source_ip, worker_name, temp_pool, recovery_pool, credentials, args)
            log.append(row)
            if row["duplicate_detected"] == "yes":
                print(f"ДУБЛЬ НАЙДЕН: {source_ip} -> {row['recovery_ip'] or row['temp_ip']} ({row['status']})")
            elif row["status"] in {"restore_failed", "temp_not_verified", "duplicate_no_recovery_ip", "recovery_failed"}:
                print(f"ПРОВЕРЬ: {source_ip} {row['status']} {row['details']}")
                if args.stop_on_error:
                    stop_event.set()
            else:
                verbose_print(args, f"{source_ip}: {row['status']} duplicate={row['duplicate_detected']} recovery={row['recovery_ip']}")
        finally:
            work_queue.task_done()


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Recover cameras hidden behind duplicate IP addresses.")
    parser.add_argument("--start-ip", default="10.53.240.30")
    parser.add_argument("--end-ip", default="10.53.240.176")
    parser.add_argument(
        "--temp-pools",
        type=parse_temp_pools,
        default=parse_temp_pools("10.53.240.180-189;10.53.240.190-199;10.53.240.200-210"),
        help="Semicolon-separated temp pools. Example: 10.53.240.180-189;10.53.240.190-199;10.53.240.200-210",
    )
    parser.add_argument("--recovery-pool", type=parse_ip_range, default=parse_ip_range("10.53.240.145-149"))
    parser.add_argument("--skip-recovery-pool", action="store_true", default=True)
    parser.add_argument("--netmask", default="255.255.252.0")
    parser.add_argument("--gateway", default="10.53.240.1")
    parser.add_argument("--primary-dns", default="10.70.200.5")
    parser.add_argument("--secondary-dns", default="10.70.200.6")
    parser.add_argument("--credential", action="append", type=dup.parse_credential, default=None)
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--api", choices=("auto", "unv", "sunell"), default="auto")
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--verify-timeout", type=float, default=40.0)
    parser.add_argument("--missing-pings", type=int, default=3)
    parser.add_argument("--output-csv", default=str(script_dir / "duplicate_ip_recovery.csv"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="Print every checked IP and low-level API output.")
    parser.add_argument("--no-stop-on-error", dest="stop_on_error", action="store_false")
    parser.set_defaults(stop_on_error=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    credentials = args.credential
    if not credentials:
        password = args.password
        if password is None:
            password = getpass.getpass(f"Password for {args.username}: ")
        credentials = [(args.username, password)]
    verbose_print(args, "Credentials to try: " + ", ".join(username for username, _ in credentials))

    recovery_pool = RecoveryPool(args.recovery_pool)
    scan_ips = iter_scan_ips(args, set(args.recovery_pool))
    work_queue = queue.Queue()
    for ip in scan_ips:
        work_queue.put(ip)

    log = CsvLog(pathlib.Path(args.output_csv))
    threads = []
    stop_event = threading.Event()
    for index, temp_pool in enumerate(args.temp_pools):
        thread = threading.Thread(
            target=worker,
            args=(index, temp_pool, work_queue, recovery_pool, credentials, args, log, stop_event),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()

    print(f"Done. Log: {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
