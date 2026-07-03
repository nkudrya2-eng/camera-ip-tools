import argparse
import base64
import csv
import datetime as dt
import getpass
import hashlib
import http.client
import ipaddress
import os
import pathlib
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


NETWORK_ERRORS = (
    urllib.error.URLError,
    urllib.error.HTTPError,
    TimeoutError,
    http.client.RemoteDisconnected,
    ConnectionError,
    OSError,
)

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
}


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
    for value in range(start, end + 1):
        yield str(ipaddress.IPv4Address(value))


def iter_target_ips(args: argparse.Namespace):
    if args.ip_list:
        for raw in args.ip_list.split(","):
            ip = raw.strip()
            if ip:
                ipaddress.IPv4Address(ip)
                yield ip
        return
    yield from iter_range(args.start_ip, args.end_ip)


def parse_credential(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("Use username:password, for example Admin:1234")
    username, password = value.split(":", 1)
    username = username.strip()
    if not username:
        raise argparse.ArgumentTypeError("Credential username is empty.")
    return username, password


def wsse_header(username: str, password: str) -> str:
    # ONVIF uses WS-Security UsernameToken digest: Base64(SHA1(nonce + created + password)).
    nonce = os.urandom(16)
    created = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    digest = hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()
    return f"""
    <s:Header>
      <wsse:Security s:mustUnderstand="1">
        <wsse:UsernameToken>
          <wsse:Username>{xml_escape(username)}</wsse:Username>
          <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{base64.b64encode(digest).decode("ascii")}</wsse:Password>
          <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode("ascii")}</wsse:Nonce>
          <wsu:Created>{created}</wsu:Created>
        </wsse:UsernameToken>
      </wsse:Security>
    </s:Header>"""


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def soap_envelope(action_xml: str, username: str | None, password: str | None) -> bytes:
    header = wsse_header(username, password) if username and password is not None else "<s:Header/>"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{NS['s']}" xmlns:tds="{NS['tds']}" xmlns:tt="{NS['tt']}" xmlns:wsse="{NS['wsse']}" xmlns:wsu="{NS['wsu']}">
  {header}
  <s:Body>
    {action_xml}
  </s:Body>
</s:Envelope>"""
    return xml.encode("utf-8")


def onvif_post(
    ip: str,
    path: str,
    username: str | None,
    password: str | None,
    timeout: float,
    action: str,
    body_xml: str,
) -> ET.Element:
    url = f"http://{ip}{path}"
    request = urllib.request.Request(
        url,
        data=soap_envelope(body_xml, username, password),
        method="POST",
        headers={
            "Content-Type": f'application/soap+xml; charset=utf-8; action="http://www.onvif.org/ver10/device/wsdl/{action}"',
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
    return ET.fromstring(text)


def text_or_empty(root: ET.Element, path: str) -> str:
    node = root.find(path, NS)
    return (node.text or "").strip() if node is not None else ""


def compact_fault(root: ET.Element) -> str:
    fault = root.find(".//s:Fault", NS)
    if fault is None:
        return ""
    texts = [item.text.strip() for item in fault.iter() if item.text and item.text.strip()]
    return " | ".join(texts)[:500]


def parse_system_time(root: ET.Element) -> dict:
    date_path = ".//tds:GetSystemDateAndTimeResponse/tds:SystemDateAndTime"
    timezone = text_or_empty(root, f"{date_path}/tt:TimeZone/tt:TZ")
    daylight = text_or_empty(root, f"{date_path}/tt:DaylightSavings")
    year = text_or_empty(root, f"{date_path}/tt:UTCDateTime/tt:Date/tt:Year")
    month = text_or_empty(root, f"{date_path}/tt:UTCDateTime/tt:Date/tt:Month")
    day = text_or_empty(root, f"{date_path}/tt:UTCDateTime/tt:Date/tt:Day")
    hour = text_or_empty(root, f"{date_path}/tt:UTCDateTime/tt:Time/tt:Hour")
    minute = text_or_empty(root, f"{date_path}/tt:UTCDateTime/tt:Time/tt:Minute")
    second = text_or_empty(root, f"{date_path}/tt:UTCDateTime/tt:Time/tt:Second")
    utc = ""
    if all([year, month, day, hour, minute, second]):
        utc = f"{year}-{int(month):02d}-{int(day):02d}T{int(hour):02d}:{int(minute):02d}:{int(second):02d}Z"
    return {"timezone": timezone, "daylight": daylight, "utc_time": utc}


def parse_ntp(root: ET.Element) -> str:
    values = []
    for node in root.findall(".//tt:NTPManual", NS) + root.findall(".//tt:NTPFromDHCP", NS):
        ipv4 = text_or_empty(node, ".//tt:IPv4Address")
        ipv6 = text_or_empty(node, ".//tt:IPv6Address")
        dns = text_or_empty(node, ".//tt:DNSname")
        item_type = text_or_empty(node, ".//tt:Type")
        value = ipv4 or ipv6 or dns
        if value:
            values.append(f"{item_type}:{value}" if item_type else value)
    return "; ".join(values)


def probe_one(ip: str, username: str | None, password: str | None, args: argparse.Namespace) -> dict:
    row = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "ip": ip,
        "ok": "0",
        "onvif_path": args.path,
        "device": "",
        "timezone": "",
        "daylight": "",
        "utc_time": "",
        "ntp": "",
        "message": "",
    }
    if not args.skip_ping and not ping(ip, args.ping_timeout_ms):
        row["message"] = "no ping reply"
        return row

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
        fault = compact_fault(time_root)
        if fault:
            row["message"] = "GetSystemDateAndTime fault: " + fault
            return row
        row.update(parse_system_time(time_root))

        try:
            info_root = onvif_post(
                ip,
                args.path,
                username,
                password,
                args.http_timeout,
                "GetDeviceInformation",
                "<tds:GetDeviceInformation/>",
            )
            model = text_or_empty(info_root, ".//tds:GetDeviceInformationResponse/tds:Model")
            serial = text_or_empty(info_root, ".//tds:GetDeviceInformationResponse/tds:SerialNumber")
            row["device"] = " ".join(part for part in [model, serial] if part)
        except NETWORK_ERRORS as exc:
            row["device"] = f"device info failed: {exc}"

        try:
            ntp_root = onvif_post(ip, args.path, username, password, args.http_timeout, "GetNTP", "<tds:GetNTP/>")
            fault = compact_fault(ntp_root)
            row["ntp"] = "fault: " + fault if fault else parse_ntp(ntp_root)
        except NETWORK_ERRORS as exc:
            row["ntp"] = f"GetNTP failed: {exc}"

        row["ok"] = "1"
        row["message"] = "ONVIF time probe OK"
        return row
    except NETWORK_ERRORS as exc:
        row["message"] = str(exc)
        return row
    except ET.ParseError as exc:
        row["message"] = f"XML parse error: {exc}"
        return row
    except Exception as exc:
        row["message"] = f"{type(exc).__name__}: {exc}"
        return row


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Probe ONVIF time and NTP settings without writing to cameras.")
    parser.add_argument("--start-ip", default="10.53.240.136")
    parser.add_argument("--end-ip", default="10.53.240.136")
    parser.add_argument("--ip-list", default="", help="Comma-separated IP list; overrides --start-ip/--end-ip.")
    parser.add_argument("--credential", type=parse_credential, default=None, help="One credential only: username:password.")
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--path", default="/onvif/device_service")
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--skip-ping", action="store_true")
    parser.add_argument("--output-csv", default=str(script_dir / "onvif_time_probe.csv"))
    parser.add_argument("--supported-csv", default=str(script_dir / "onvif_supported.csv"))
    parser.add_argument("--refused-csv", default=str(script_dir / "onvif_refused.csv"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.credential:
        username, password = args.credential
    else:
        username = args.username
        password = args.password
        if password is None:
            password = getpass.getpass(f"Password for {username}: ")

    rows = []
    for ip in iter_target_ips(args):
        print(f"{ip}: probing ONVIF time")
        row = probe_one(ip, username, password, args)
        print(f"{ip}: {'OK' if row['ok'] == '1' else 'FAILED'} {row['message']}")
        rows.append(row)

    output_path = pathlib.Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp",
        "ip",
        "ok",
        "onvif_path",
        "device",
        "timezone",
        "daylight",
        "utc_time",
        "ntp",
        "message",
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )
        writer.writeheader()
        writer.writerows(rows)

    supported_path = pathlib.Path(args.supported_csv)
    refused_path = pathlib.Path(args.refused_csv)
    for path, selected_rows in [
        (supported_path, [row for row in rows if row["ok"] == "1"]),
        (refused_path, [row for row in rows if row["ok"] != "1"]),
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(selected_rows)
    print(f"Written {len(rows)} rows to {output_path}")
    print(f"Supported ONVIF: {supported_path}")
    print(f"Refused/no ONVIF: {refused_path}")
    print(f"OK: {sum(1 for row in rows if row['ok'] == '1')}, failed: {sum(1 for row in rows if row['ok'] != '1')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
