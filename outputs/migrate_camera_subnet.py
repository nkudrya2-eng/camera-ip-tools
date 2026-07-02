import argparse
import csv
import datetime as dt
import getpass
import pathlib
import sys
import time
from types import SimpleNamespace

import configure_cameras_loop as camera_tools


def build_ip(prefix: str, last_octet: int) -> str:
    return f"{prefix}.{last_octet}"


def check_prefix(value: str) -> str:
    parts = value.split(".")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("Use three octets, for example 10.86.240")
    try:
        octets = [int(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid prefix: {value}") from exc
    if any(octet < 0 or octet > 255 for octet in octets):
        raise argparse.ArgumentTypeError(f"Invalid prefix: {value}")
    return ".".join(str(octet) for octet in octets)


def make_camera_args(args: argparse.Namespace, source_ip: str, username: str) -> SimpleNamespace:
    return SimpleNamespace(
        camera_ip=source_ip,
        username=username,
        netmask=args.netmask,
        gateway=args.gateway,
        primary_dns=args.primary_dns,
        secondary_dns=args.secondary_dns,
        ping_timeout_ms=args.ping_timeout_ms,
        http_timeout=args.http_timeout,
        poll_seconds=args.poll_seconds,
        verify_timeout=args.verify_timeout,
        dry_run=args.dry_run,
    )


def target_ip_is_free(args: argparse.Namespace, target_ip: str) -> bool:
    camera_tools.flush_arp(target_ip)
    if camera_tools.ping(target_ip, args.ping_timeout_ms):
        print(f"{target_ip}: busy, skipped")
        return False
    print(f"{target_ip}: free")
    return True


def choose_api(camera_args: SimpleNamespace, password: str, requested_api: str) -> str:
    if requested_api != "auto":
        return requested_api

    info = camera_tools.collect_camera_info(camera_args, password)
    if camera_tools.network_interface_is_supported(info):
        return "unv"

    print("LAPI is not available; using Sunell CGI.")
    return "sunell"


def append_log(args: argparse.Namespace, row: dict) -> None:
    log_path = pathlib.Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    exists = log_path.exists()
    fieldnames = [
        "timestamp",
        "source_ip",
        "target_ip",
        "api",
        "status",
        "message",
    ]

    with log_path.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def log_result(
    args: argparse.Namespace,
    source_ip: str,
    target_ip: str,
    api: str,
    status: str,
    message: str,
    username: str = "",
) -> None:
    append_log(
        args,
        {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "source_ip": source_ip,
            "target_ip": target_ip,
            "api": api,
            "status": status,
            "message": f"{message}; username={username}" if username else message,
        },
    )


def parse_credential(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("Use username:password, for example Admin:password")
    username, password = value.split(":", 1)
    if not username:
        raise argparse.ArgumentTypeError("Credential username is empty.")
    return username, password


def get_credentials(args: argparse.Namespace) -> list[tuple[str, str]]:
    if args.credential:
        return args.credential

    password = args.password
    if password is None and not args.dry_run:
        password = getpass.getpass(f"Password for {args.username}: ")
    return [(args.username, password or "")]


def try_migrate_with_credential(
    args: argparse.Namespace,
    source_ip: str,
    target_ip: str,
    username: str,
    password: str,
) -> bool:
    camera_args = make_camera_args(args, source_ip, username)
    api = choose_api(camera_args, password, args.api)

    try:
        ok = camera_tools.send_camera_command(camera_args, password, target_ip, api)
    except camera_tools.NETWORK_ERRORS as exc:
        print(f"{source_ip}: {username}: request failed: {exc}", file=sys.stderr)
        log_result(args, source_ip, target_ip, api, "failed", str(exc), username)
        return False

    camera_tools.flush_arp(source_ip)
    camera_tools.flush_arp(target_ip)

    if not ok:
        print(f"{source_ip}: {username}: command failed")
        log_result(args, source_ip, target_ip, api, "failed", "camera returned an error", username)
        return False

    if not args.no_verify_new_ip and not camera_tools.wait_until_new_ip_found(camera_args, target_ip):
        log_result(args, source_ip, target_ip, api, "unverified", "new IP did not reply", username)
        return False

    print(f"{source_ip}: changed to {target_ip} with {username}")
    log_result(args, source_ip, target_ip, api, "changed", "ok", username)
    return True


def target_last_octet(args: argparse.Namespace, source_last_octet: int) -> int:
    if args.target_start is None:
        return source_last_octet
    return args.target_start + (source_last_octet - args.start)


def migrate_one(args: argparse.Namespace, credentials: list[tuple[str, str]], last_octet: int) -> bool:
    source_ip = build_ip(args.source_prefix, last_octet)
    target_ip = build_ip(args.target_prefix, target_last_octet(args, last_octet))

    camera_tools.flush_arp(source_ip)
    if not camera_tools.ping(source_ip, args.ping_timeout_ms):
        print(f"{source_ip}: no ping reply")
        return False

    print(f"{source_ip}: found, target {target_ip}")
    if not args.no_free_check and not target_ip_is_free(args, target_ip):
        log_result(args, source_ip, target_ip, "", "skipped", "target IP is busy")
        return False

    for username, password in credentials:
        print(f"{source_ip}: trying username {username}")
        if try_migrate_with_credential(args, source_ip, target_ip, username, password):
            return True

    print(f"{source_ip}: all credentials failed")
    return False


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Move cameras from one /24 prefix to another, preserving last octet.")
    parser.add_argument("--source-prefix", default="10.86.240", type=check_prefix)
    parser.add_argument("--target-prefix", default="10.53.240", type=check_prefix)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=254)
    parser.add_argument(
        "--target-start",
        type=int,
        default=None,
        help="First last-octet to assign in target prefix. Default: preserve source last-octet.",
    )
    parser.add_argument("--netmask", default="255.255.255.0")
    parser.add_argument("--gateway", default="10.53.240.1")
    parser.add_argument("--primary-dns", default="128.0.0.1")
    parser.add_argument("--secondary-dns", default="128.0.0.2")
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--password", default=None)
    parser.add_argument(
        "--credential",
        action="append",
        type=parse_credential,
        default=None,
        help="Credential pair username:password. Can be used more than once.",
    )
    parser.add_argument("--api", choices=("auto", "unv", "sunell"), default="auto")
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--verify-timeout", type=float, default=30)
    parser.add_argument("--no-free-check", action="store_true")
    parser.add_argument("--no-verify-new-ip", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pause-after-change", type=float, default=1.0)
    parser.add_argument("--log-file", default=str(script_dir / "camera_subnet_migration.csv"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.start < 1 or args.end > 254 or args.end < args.start:
        raise SystemExit("Use --start/--end in range 1..254.")
    if args.target_start is not None:
        target_end = args.target_start + (args.end - args.start)
        if args.target_start < 1 or target_end > 254:
            raise SystemExit("--target-start would produce IPs outside range 1..254.")

    credentials = get_credentials(args)

    changed = 0
    found = 0
    for last_octet in range(args.start, args.end + 1):
        source_ip = build_ip(args.source_prefix, last_octet)
        if camera_tools.ping(source_ip, args.ping_timeout_ms):
            found += 1
        if migrate_one(args, credentials, last_octet):
            changed += 1
            time.sleep(args.pause_after_change)

    print(f"Finished. Found: {found}. Changed: {changed}. Log: {args.log_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
