import argparse
import csv
import datetime as dt
import getpass
import ipaddress
import pathlib

from probe_onvif_time import (
    NETWORK_ERRORS,
    compact_fault,
    iter_range,
    onvif_post,
    parse_credential,
    parse_ntp,
    parse_system_time,
    ping,
    xml_escape,
)


def iter_target_ips(args: argparse.Namespace):
    if args.ip_list:
        for raw in args.ip_list.split(","):
            ip = raw.strip()
            if ip:
                ipaddress.IPv4Address(ip)
                yield ip
        return
    yield from iter_range(args.start_ip, args.end_ip)


def offset_to_onvif_tz(offset: str) -> str:
    raw = offset.strip()
    if raw.upper().startswith("UTC"):
        return raw
    if not raw or raw[0] not in "+-":
        raise argparse.ArgumentTypeError("Use timezone like +08:00, +09:00, or explicit UTC-08:00:00.")
    sign = raw[0]
    parts = raw[1:].split(":")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError("Use timezone like +08:00.")
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = int(parts[2]) if len(parts) > 2 else 0
    if hours > 23 or minutes > 59 or seconds > 59:
        raise argparse.ArgumentTypeError("Invalid timezone offset.")

    # ONVIF uses POSIX-like TZ strings on many cameras: UTC-08:00 means local UTC+08.
    onvif_sign = "-" if sign == "+" else "+"
    return f"UTC{onvif_sign}{hours:02d}:{minutes:02d}:{seconds:02d}"


def set_system_time_xml(timezone: str, daylight_savings: bool) -> str:
    daylight = "true" if daylight_savings else "false"
    return f"""
<tds:SetSystemDateAndTime>
  <tds:DateTimeType>NTP</tds:DateTimeType>
  <tds:DaylightSavings>{daylight}</tds:DaylightSavings>
  <tds:TimeZone>
    <tt:TZ>{xml_escape(timezone)}</tt:TZ>
  </tds:TimeZone>
</tds:SetSystemDateAndTime>"""


def set_ntp_xml(ntp_server: str, from_dhcp: bool) -> str:
    dhcp = "true" if from_dhcp else "false"
    return f"""
<tds:SetNTP>
  <tds:FromDHCP>{dhcp}</tds:FromDHCP>
  <tds:NTPManual>
    <tt:Type>IPv4</tt:Type>
    <tt:IPv4Address>{xml_escape(ntp_server)}</tt:IPv4Address>
  </tds:NTPManual>
</tds:SetNTP>"""


def probe_after(ip: str, username: str, password: str, args: argparse.Namespace) -> tuple[str, str, str, str]:
    errors = []
    timezone = ""
    utc_time = ""
    ntp = ""

    try:
        time_root = onvif_post(
            ip,
            args.path,
            username,
            password,
            args.http_timeout,
            "GetSystemDateAndTime",
            "<tds:GetSystemDateAndTime/>",
        )
        time_data = parse_system_time(time_root)
        timezone = time_data.get("timezone", "")
        utc_time = time_data.get("utc_time", "")
    except NETWORK_ERRORS as exc:
        errors.append(f"GetSystemDateAndTime: {exc}")

    try:
        ntp_root = onvif_post(ip, args.path, username, password, args.http_timeout, "GetNTP", "<tds:GetNTP/>")
        ntp = parse_ntp(ntp_root)
    except NETWORK_ERRORS as exc:
        errors.append(f"GetNTP: {exc}")

    return timezone, utc_time, ntp, "; ".join(errors)


def write_onvif(ip: str, username: str, password: str, args: argparse.Namespace, action: str, body_xml: str) -> tuple[str, str]:
    try:
        root = onvif_post(ip, args.path, username, password, args.http_timeout, action, body_xml)
    except NETWORK_ERRORS as exc:
        return "failed", str(exc)
    except Exception as exc:
        return "failed", f"{type(exc).__name__}: {exc}"

    fault = compact_fault(root)
    if fault:
        return "failed", fault
    return "ok", "OK"


def apply_one(ip: str, username: str, password: str, args: argparse.Namespace) -> dict:
    row = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "ip": ip,
        "ok": "0",
        "timezone_requested": args.timezone,
        "timezone_onvif": args.timezone_onvif,
        "ntp_server": args.ntp_server,
        "ntp_write": "",
        "timezone_write": "",
        "timezone_after": "",
        "utc_time_after": "",
        "ntp_after": "",
        "message": "",
    }
    if not args.skip_ping and not ping(ip, args.ping_timeout_ms):
        row["message"] = "no ping reply"
        return row

    if not args.apply:
        row["message"] = "dry-run; add --apply to write"
        row["ntp_write"] = "dry-run"
        row["timezone_write"] = "dry-run"
        row["timezone_after"] = args.timezone_onvif
        row["ntp_after"] = args.ntp_server
        return row

    messages = []
    if args.skip_ntp:
        row["ntp_write"] = "skipped"
    else:
        row["ntp_write"], message = write_onvif(
            ip,
            username,
            password,
            args,
            "SetNTP",
            set_ntp_xml(args.ntp_server, args.from_dhcp),
        )
        if row["ntp_write"] != "ok":
            messages.append("SetNTP: " + message)

    if args.skip_timezone:
        row["timezone_write"] = "skipped"
    else:
        row["timezone_write"], message = write_onvif(
            ip,
            username,
            password,
            args,
            "SetSystemDateAndTime",
            set_system_time_xml(args.timezone_onvif, args.daylight_savings),
        )
        if row["timezone_write"] != "ok":
            messages.append("SetSystemDateAndTime: " + message)

    timezone_after, utc_after, ntp_after, verify_errors = probe_after(ip, username, password, args)
    row["timezone_after"] = timezone_after
    row["utc_time_after"] = utc_after
    row["ntp_after"] = ntp_after

    timezone_ok = args.skip_timezone or timezone_after == args.timezone_onvif
    ntp_ok = args.skip_ntp or args.ntp_server in ntp_after
    if timezone_ok and ntp_ok:
        row["ok"] = "1"
        row["message"] = "ONVIF target verified"
        return row

    if verify_errors:
        messages.append("verify: " + verify_errors)
    if not timezone_ok:
        messages.append(f"timezone after is {timezone_after or '<empty>'}")
    if not ntp_ok:
        messages.append(f"NTP after is {ntp_after or '<empty>'}")
    row["message"] = "; ".join(messages) if messages else "target not verified"
    return row


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Set ONVIF NTP and timezone on cameras.")
    parser.add_argument("--start-ip", default="10.53.240.136")
    parser.add_argument("--end-ip", default="10.53.240.136")
    parser.add_argument("--ip-list", default="", help="Comma-separated IP list; overrides --start-ip/--end-ip.")
    parser.add_argument("--credential", type=parse_credential, default=None, help="One credential only: username:password.")
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--path", default="/onvif/device_service")
    parser.add_argument("--ntp-server", default="10.99.200.60")
    parser.add_argument("--timezone", default="+08:00", help="Human offset, for example +08:00.")
    parser.add_argument("--timezone-onvif", default="", help="Explicit ONVIF TZ, for example UTC-08:00:00.")
    parser.add_argument("--from-dhcp", action="store_true", help="Use NTP from DHCP instead of manual NTP.")
    parser.add_argument("--daylight-savings", action="store_true")
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--skip-ping", action="store_true")
    parser.add_argument("--skip-ntp", action="store_true", help="Do not write NTP; still write/verify timezone.")
    parser.add_argument("--skip-timezone", action="store_true", help="Do not write timezone; still write/verify NTP.")
    parser.add_argument("--apply", action="store_true", help="Actually write settings. Without this flag it is dry-run.")
    parser.add_argument("--output-csv", default=str(script_dir / "onvif_time_set_results.csv"))
    args = parser.parse_args()
    if not args.timezone_onvif:
        args.timezone_onvif = offset_to_onvif_tz(args.timezone)
    return args


def main() -> int:
    args = parse_args()
    if args.credential:
        username, password = args.credential
    else:
        username = args.username
        password = args.password
        if password is None:
            password = getpass.getpass(f"Password for {username}: ")

    print(f"Target NTP: {args.ntp_server}")
    print(f"Target timezone: {args.timezone} -> ONVIF {args.timezone_onvif}")
    if not args.apply:
        print("DRY-RUN: no writes. Add --apply to write settings.")

    rows = []
    for ip in iter_target_ips(args):
        print(f"{ip}: setting ONVIF time/NTP" if args.apply else f"{ip}: dry-run ONVIF time/NTP")
        row = apply_one(ip, username, password, args)
        print(f"{ip}: {'OK' if row['ok'] == '1' else 'FAILED'} {row['message']}")
        rows.append(row)

    output_path = pathlib.Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "timestamp",
                "ip",
                "ok",
                "timezone_requested",
                "timezone_onvif",
                "ntp_server",
                "ntp_write",
                "timezone_write",
                "timezone_after",
                "utc_time_after",
                "ntp_after",
                "message",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Written {len(rows)} rows to {output_path}")
    print(f"OK: {sum(1 for row in rows if row['ok'] == '1')}, failed: {sum(1 for row in rows if row['ok'] != '1')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
