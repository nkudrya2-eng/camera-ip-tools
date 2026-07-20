import argparse
import base64
import concurrent.futures
import csv
import datetime as dt
import getpass
import ipaddress
import json
import os
import pathlib
import re
import select
import socket
import subprocess
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET


SUNELL_GROUP = "234.5.6.7"
SUNELL_REQUEST_PORT = 31001
SUNELL_RESPONSE_PORT = 31002
ONVIF_GROUP = "239.255.255.250"
ONVIF_PORT = 3702
SUNELL_DISCOVERY = bytes.fromhex(
    "AF AF 00 01 00 00 00 00 00 00 77 4B 00 00 00 00 00 00 00 43 00 00 7E 23 "
    "3C 3F 78 6D 6C 20 76 65 72 73 69 6F 6E 3D 22 31 2E 30 22 20 65 6E 63 6F "
    "64 69 6E 67 3D 22 55 54 46 2D 38 22 3F 3E 0D 0A 3C 50 61 72 61 6D 65 74 "
    "65 72 73 20 56 65 72 73 69 6F 6E 3D 22 31 22 20 2F 3E 0A"
)

CSV_FIELDS = [
    "selected",
    "protocol",
    "current_ip",
    "new_ip",
    "model",
    "mac",
    "device_id",
    "serial_number",
    "device_name",
    "manufacturer",
    "firmware",
    "http_port",
    "mask",
    "gateway",
    "dns",
    "status",
    "message",
    "raw_attributes",
]


def local_ipv4_addresses(explicit: list[str]) -> list[str]:
    addresses = set(explicit)
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.AddressState -eq 'Preferred' -and $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*'} | Select-Object -ExpandProperty IPAddress",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            addresses.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(item[4][0])
    except socket.gaierror:
        pass

    valid = []
    for address in addresses:
        try:
            parsed = ipaddress.IPv4Address(address)
        except ipaddress.AddressValueError:
            continue
        if not parsed.is_loopback and not parsed.is_link_local:
            valid.append(str(parsed))
    return sorted(set(valid), key=lambda value: int(ipaddress.IPv4Address(value)))


def parse_sunell_packet(data: bytes, source_ip: str) -> dict | None:
    start = data.find(b"<?xml")
    if start < 0:
        return None
    try:
        root = ET.fromstring(data[start:].rstrip(b"\x00"))
    except ET.ParseError:
        return None
    node = root.find(".//DeviceSummaryInfo")
    if node is None:
        return None
    attrs = dict(node.attrib)
    return {
        "selected": "1",
        "protocol": "sunell",
        "current_ip": attrs.get("DeviceIP", source_ip),
        "new_ip": "",
        "model": attrs.get("ProductModel", ""),
        "mac": attrs.get("MACAddr", ""),
        "device_id": attrs.get("DeviceId", ""),
        "serial_number": attrs.get("SN", ""),
        "device_name": attrs.get("DeviceName", ""),
        "manufacturer": attrs.get("ManufacturerName", ""),
        "firmware": attrs.get("SoftWareInfo", ""),
        "http_port": attrs.get("HttpPort", ""),
        "mask": attrs.get("SubNetMask", attrs.get("SubnetMask", "")),
        "gateway": attrs.get("Gateway", ""),
        "dns": attrs.get("PrimaryDNS", ""),
        "status": "discovered",
        "message": "",
        "raw_attributes": json.dumps(attrs, ensure_ascii=False, separators=(",", ":")),
    }


def scan_sunell(interface_ips: list[str], timeout: float, repeats: int) -> list[dict]:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    receiver.bind(("", SUNELL_RESPONSE_PORT))
    receiver.settimeout(0.25)
    for interface_ip in interface_ips:
        try:
            membership = socket.inet_aton(SUNELL_GROUP) + socket.inet_aton(interface_ip)
            receiver.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        except OSError:
            pass

    senders = []
    for interface_ip in interface_ips:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
        senders.append(sender)

    found = {}
    deadline = time.monotonic() + timeout
    next_send = 0.0
    sends_left = max(1, repeats)
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if sends_left and now >= next_send:
                for sender in senders:
                    sender.sendto(SUNELL_DISCOVERY, (SUNELL_GROUP, SUNELL_REQUEST_PORT))
                sends_left -= 1
                next_send = now + 0.7
            try:
                data, address = receiver.recvfrom(65535)
            except socket.timeout:
                continue
            row = parse_sunell_packet(data, address[0])
            if row:
                key = (row["mac"].lower(), row["device_id"], row["current_ip"])
                found[key] = row
    finally:
        receiver.close()
        for sender in senders:
            sender.close()
    return sorted(found.values(), key=lambda row: int(ipaddress.IPv4Address(row["current_ip"])))


def parse_sdk_row(value: str, protocol: str) -> dict | None:
    fields = next(csv.reader([value]))
    if len(fields) < 8:
        return None
    model, mac, current_ip, mask, firmware, manufactured, host_name, http_port = fields[:8]
    try:
        ipaddress.IPv4Address(current_ip)
    except ipaddress.AddressValueError:
        return None
    attrs = {
        "model": model,
        "mac": mac,
        "ip": current_ip,
        "mask": mask,
        "firmware": firmware,
        "manufactured": manufactured,
        "host_name": host_name,
        "http_port": http_port,
    }
    return {
        "selected": "1",
        "protocol": protocol,
        "current_ip": current_ip,
        "new_ip": "",
        "model": model,
        "mac": mac,
        "device_id": "",
        "serial_number": "",
        "device_name": host_name,
        "manufacturer": protocol,
        "firmware": firmware,
        "http_port": http_port,
        "mask": mask,
        "gateway": "",
        "dns": "",
        "status": "discovered",
        "message": "",
        "raw_attributes": json.dumps(attrs, ensure_ascii=False, separators=(",", ":")),
    }


def scan_vendor_sdk(executable: pathlib.Path, vendor_dir: pathlib.Path, timeout: float) -> list[dict]:
    environment = dict(os.environ)
    environment["ESC_VENDOR_DIR"] = str(vendor_dir)
    process = subprocess.run(
        [str(executable), "scan-all"],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=vendor_dir,
        env=environment,
    )
    if process.returncode != 0:
        raise RuntimeError((process.stderr or process.stdout).strip())
    rows = []
    for line in process.stdout.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3 or fields[0] != "RESULT":
            continue
        protocol = fields[1]
        encoded = fields[2]
        try:
            value = base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        row = parse_sdk_row(value, protocol)
        if row:
            rows.append(row)
    return rows


def xml_text_by_local_name(root: ET.Element, local_name: str) -> str:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == local_name and element.text:
            return element.text.strip()
    return ""


def onvif_scope_value(scopes: list[str], name: str) -> str:
    for scope in scopes:
        parsed = urllib.parse.urlsplit(scope)
        marker = f"/{name}/"
        if parsed.hostname and parsed.hostname.endswith("onvif.org") and marker in parsed.path:
            return urllib.parse.unquote(parsed.path.split(marker, 1)[1]).replace("_", " ")
    return ""


def parse_onvif_packet(data: bytes, source_ip: str) -> dict | None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    xaddrs = xml_text_by_local_name(root, "XAddrs").split()
    endpoint = xml_text_by_local_name(root, "Address")
    scopes = xml_text_by_local_name(root, "Scopes").split()
    current_ip = source_ip
    for address in xaddrs:
        try:
            host = urllib.parse.urlsplit(address).hostname
            if host:
                current_ip = host
                break
        except ValueError:
            continue
    try:
        ipaddress.IPv4Address(current_ip)
    except ipaddress.AddressValueError:
        return None
    attrs = {"endpoint": endpoint, "xaddrs": xaddrs, "scopes": scopes}
    endpoint_hex = re.search(r"([0-9a-fA-F]{12})$", endpoint)
    mac = ""
    if endpoint_hex:
        compact_mac = endpoint_hex.group(1).upper()
        mac = ":".join(compact_mac[index : index + 2] for index in range(0, 12, 2))
    return {
        "selected": "0",
        "protocol": "onvif",
        "current_ip": current_ip,
        "new_ip": "",
        "model": onvif_scope_value(scopes, "model") or onvif_scope_value(scopes, "hardware"),
        "mac": mac,
        "device_id": endpoint,
        "serial_number": "",
        "device_name": onvif_scope_value(scopes, "name"),
        "manufacturer": "",
        "firmware": "",
        "http_port": "",
        "mask": "",
        "gateway": "",
        "dns": "",
        "status": "discovered",
        "message": "ONVIF discovery only; credentials are required for full details",
        "raw_attributes": json.dumps(attrs, ensure_ascii=False, separators=(",", ":")),
    }


def scan_onvif(interface_ips: list[str], timeout: float, repeats: int) -> list[dict]:
    sockets = []
    for interface_ip in interface_ips:
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        client.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        client.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
        client.bind((interface_ip, 0))
        client.setblocking(False)
        sockets.append(client)

    found = {}
    deadline = time.monotonic() + timeout
    next_send = 0.0
    sends_left = max(1, repeats)
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if sends_left and now >= next_send:
                message_id = f"urn:uuid:{uuid.uuid4()}"
                probe = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                    'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
                    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
                    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
                    f'<s:Header><a:MessageID>{message_id}</a:MessageID>'
                    '<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>'
                    '<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>'
                    '</s:Header><s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>'
                    '</d:Probe></s:Body></s:Envelope>'
                ).encode("utf-8")
                for client in sockets:
                    client.sendto(probe, (ONVIF_GROUP, ONVIF_PORT))
                sends_left -= 1
                next_send = now + 0.7
            readable, _, _ = select.select(sockets, [], [], 0.25)
            for client in readable:
                try:
                    data, address = client.recvfrom(65535)
                except OSError:
                    continue
                row = parse_onvif_packet(data, address[0])
                if row:
                    key = row["device_id"] or row["current_ip"]
                    found[key] = row
    finally:
        for client in sockets:
            client.close()
    return list(found.values())


def merge_discovery_rows(rows: list[dict]) -> list[dict]:
    found = {}
    vendor_rows = [row for row in rows if row.get("protocol") != "onvif"]
    onvif_rows = [row for row in rows if row.get("protocol") == "onvif"]
    for row in vendor_rows:
        key = row.get("mac", "").replace(":", "").replace("-", "").lower()
        if not key:
            key = row.get("protocol", "") + ":" + row.get("current_ip", "")
        existing = found.get(key)
        if existing is None or (row.get("protocol") == "sunell" and existing.get("protocol") != "sunell"):
            found[key] = row
    known_ips = {}
    for row in found.values():
        known_ips.setdefault(row["current_ip"], []).append(row)
    for row in onvif_rows:
        if len(known_ips.get(row["current_ip"], [])) == 1:
            existing = known_ips[row["current_ip"]][0]
            if not existing.get("model"):
                existing["model"] = row["model"]
            if not existing.get("device_name"):
                existing["device_name"] = row["device_name"]
            continue
        key = "onvif:" + (row.get("device_id") or row["current_ip"])
        found[key] = row
    return sorted(found.values(), key=lambda row: int(ipaddress.IPv4Address(row["current_ip"])))


def read_csv(path: pathlib.Path) -> list[dict]:
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))


def write_csv(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def command_scan(args: argparse.Namespace) -> int:
    interfaces = local_ipv4_addresses(args.interface_ip)
    if not interfaces:
        raise SystemExit("No usable local IPv4 interfaces found. Use --interface-ip.")
    print("Interfaces: " + ", ".join(interfaces))
    rows = scan_sunell(interfaces, args.timeout, args.repeats)
    if not args.skip_dynacolor:
        try:
            executable = compile_bridge()
            rows.extend(scan_vendor_sdk(executable, pathlib.Path(args.vendor_dir), args.vendor_timeout))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"ESC SDK discovery failed: {exc}")
    if not args.skip_onvif:
        rows.extend(scan_onvif(interfaces, args.onvif_timeout, args.repeats))
    rows = merge_discovery_rows(rows)
    write_csv(pathlib.Path(args.output), rows)
    for row in rows:
        identity = f"DeviceId={row['device_id']}" if row["device_id"] else row["protocol"]
        print(f"{row['current_ip']}  {row['mac']}  {row['model']}  {identity}")
    print(f"Found {len(rows)} cameras. Written: {args.output}")
    counts = {}
    for row in rows:
        counts[row["protocol"]] = counts.get(row["protocol"], 0) + 1
    print("Protocols: " + ", ".join(f"{name}={count}" for name, count in sorted(counts.items())))
    return 0


def command_scan_pcap(args: argparse.Namespace) -> int:
    tshark = pathlib.Path(args.tshark)
    if not tshark.exists():
        raise SystemExit(f"tshark not found: {tshark}")
    result = subprocess.run(
        [
            str(tshark),
            "-r",
            args.pcap,
            "-Y",
            "udp.srcport == 31002",
            "-T",
            "fields",
            "-E",
            "separator=\t",
            "-e",
            "ip.src",
            "-e",
            "udp.payload",
        ],
        capture_output=True,
        text=True,
        timeout=args.command_timeout,
    )
    if result.returncode != 0:
        raise SystemExit("tshark failed: " + result.stderr.strip())

    found = {}
    for line in result.stdout.splitlines():
        source_ip, separator, payload_hex = line.partition("\t")
        if not separator or not payload_hex:
            continue
        try:
            row = parse_sunell_packet(bytes.fromhex(payload_hex), source_ip)
        except ValueError:
            continue
        if row:
            key = (row["mac"].lower(), row["device_id"], row["current_ip"])
            found[key] = row
    rows = sorted(found.values(), key=lambda row: int(ipaddress.IPv4Address(row["current_ip"])))
    write_csv(pathlib.Path(args.output), rows)
    print(f"Parsed {len(rows)} cameras from {args.pcap}. Written: {args.output}")
    return 0


def command_assign(args: argparse.Namespace) -> int:
    rows = read_csv(pathlib.Path(args.input))
    start = int(ipaddress.IPv4Address(args.start_ip))
    end = int(ipaddress.IPv4Address(args.end_ip))
    pool = [str(ipaddress.IPv4Address(value)) for value in range(start, end + 1)]
    selected = [row for row in rows if row.get("selected", "1").strip().lower() not in {"0", "no", "false"}]
    if len(selected) > len(pool):
        raise SystemExit(f"Pool has {len(pool)} addresses, but {len(selected)} cameras are selected.")
    for row, new_ip in zip(selected, pool):
        row["new_ip"] = new_ip
        row["mask"] = args.mask
        row["gateway"] = args.gateway
        row["dns"] = args.dns
        row["status"] = "planned"
        row["message"] = ""
    write_csv(pathlib.Path(args.output), rows)
    print(f"Planned {len(selected)} cameras. Written: {args.output}")
    return 0


def ping(ip: str, timeout_ms: int = 500) -> bool:
    result = subprocess.run(
        ["ping", "-n", "1", "-w", str(timeout_ms), ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def bridge_paths() -> tuple[pathlib.Path, pathlib.Path]:
    base = pathlib.Path(__file__).resolve().parent / "vendor_bridge"
    return base / "VendorBridge.cs", base / "VendorBridge.exe"


def default_vendor_dir(script_dir: pathlib.Path) -> str:
    configured = os.environ.get("ESC_VENDOR_DIR", "").strip()
    if configured:
        return configured
    portable = script_dir / "vendor"
    if portable.is_dir():
        return str(portable)
    return r"C:\Program Files (x86)\ESC\lib\Starter"


def compile_bridge() -> pathlib.Path:
    source, executable = bridge_paths()
    if executable.exists() and executable.stat().st_mtime >= source.stat().st_mtime:
        return executable
    csc = pathlib.Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe"
    if not csc.exists():
        raise SystemExit(f"32-bit C# compiler not found: {csc}")
    result = subprocess.run(
        [str(csc), "/nologo", "/target:exe", "/platform:x86", f"/out:{executable}", str(source)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit("Vendor bridge compilation failed:\n" + result.stdout + result.stderr)
    return executable


def parse_credential(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("Use username:password")
    return tuple(value.split(":", 1))


def apply_one(row: dict, executable: pathlib.Path, vendor_dir: pathlib.Path, username: str, password: str, args: argparse.Namespace) -> dict:
    result_row = dict(row)
    protocol = row.get("protocol", "sunell").strip().lower() or "sunell"
    if protocol == "dynacolor":
        command = [
            str(executable),
            "set-dynacolor",
            row["new_ip"],
            row["mask"],
            row["gateway"],
            row["dns"],
            row.get("http_port", "80") or "80",
            row["model"],
            row.get("device_name", ""),
            row["mac"],
        ]
        command_input = None
    elif protocol == "sunell":
        command = [
            str(executable),
            "set-sunell",
            row["current_ip"],
            row["new_ip"],
            row["mask"],
            row["gateway"],
            row["dns"],
            row["model"],
            row["mac"],
            row["device_id"],
            username,
        ]
        command_input = password + "\n"
    else:
        result_row["status"] = "failed"
        result_row["message"] = f"IP assignment is not implemented for ESC protocol: {protocol}"
        return result_row
    environment = dict(os.environ)
    environment["ESC_VENDOR_DIR"] = str(vendor_dir)
    try:
        process = subprocess.run(
            command,
            input=command_input,
            capture_output=True,
            text=True,
            timeout=args.command_timeout,
            cwd=vendor_dir,
            env=environment,
        )
    except subprocess.SubprocessError as exc:
        result_row["status"] = "failed"
        result_row["message"] = str(exc)
        return result_row
    if process.returncode != 0:
        result_row["status"] = "failed"
        result_row["message"] = (process.stderr or process.stdout).strip()
        return result_row

    deadline = time.monotonic() + args.verify_seconds
    while time.monotonic() < deadline:
        if ping(row["new_ip"]):
            result_row["status"] = "verified"
            result_row["message"] = "new IP replies"
            return result_row
        time.sleep(1)
    result_row["status"] = "sent_not_verified"
    result_row["message"] = "vendor command sent; new IP did not reply before timeout"
    return result_row


def command_apply(args: argparse.Namespace) -> int:
    rows = read_csv(pathlib.Path(args.input))
    selected = [
        row
        for row in rows
        if row.get("selected", "1").strip().lower() not in {"0", "no", "false"} and row.get("new_ip", "").strip()
    ]
    if not selected:
        raise SystemExit("No selected rows with new_ip.")

    targets = []
    for row in selected:
        required = ["current_ip", "new_ip", "mask", "gateway", "dns", "mac", "model"]
        if (row.get("protocol", "sunell").strip().lower() or "sunell") == "sunell":
            required.append("device_id")
        for field in required:
            if not row.get(field, "").strip():
                raise SystemExit(f"Missing {field} for camera {row.get('mac', '')}.")
        ipaddress.IPv4Address(row["current_ip"])
        ipaddress.IPv4Address(row["new_ip"])
        targets.append(row["new_ip"])
    if len(targets) != len(set(targets)):
        raise SystemExit("Duplicate new_ip values in plan.")

    if not args.apply:
        for row in selected:
            print(f"DRY-RUN {row['current_ip']} [{row['device_id']}] -> {row['new_ip']}")
        print("No commands sent. Add --apply to execute.")
        return 0

    occupied = [ip for ip in targets if ping(ip)]
    if occupied and not args.allow_occupied:
        raise SystemExit("Target addresses reply to ping: " + ", ".join(occupied) + ". Use --allow-occupied only after checking duplicates.")

    if args.credential:
        username, password = args.credential
    else:
        username = args.username
        password = getpass.getpass(f"Password for {username}: ")

    executable = compile_bridge()
    vendor_dir = pathlib.Path(args.vendor_dir)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_rows = {
            pool.submit(apply_one, row, executable, vendor_dir, username, password, args): row for row in selected
        }
        for future in concurrent.futures.as_completed(future_rows):
            result = future.result()
            results.append(result)
            print(f"{result['current_ip']} -> {result['new_ip']}: {result['status']}")

    by_key = {(row["mac"].lower(), row["device_id"]): row for row in results}
    for index, row in enumerate(rows):
        key = (row.get("mac", "").lower(), row.get("device_id", ""))
        if key in by_key:
            rows[index] = by_key[key]
    output = pathlib.Path(args.output)
    write_csv(output, rows)
    print(f"Written results: {output}")
    failed = sum(1 for row in results if row["status"] == "failed")
    return 1 if failed else 0


def command_audit_time(args: argparse.Namespace) -> int:
    from audit_camera_time import audit_inventory

    return audit_inventory(args)


def build_parser() -> argparse.ArgumentParser:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Bulk Sunell/Evidence discovery and IP assignment using ESC vendor protocol.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Discover Sunell/Evidence cameras and write CSV inventory.")
    scan.add_argument("--interface-ip", action="append", default=[], help="Local IPv4 to use; may be repeated.")
    scan.add_argument("--timeout", type=float, default=4.0)
    scan.add_argument("--repeats", type=int, default=3)
    scan.add_argument("--vendor-dir", default=default_vendor_dir(script_dir))
    scan.add_argument("--vendor-timeout", type=float, default=30.0)
    scan.add_argument("--skip-dynacolor", action="store_true")
    scan.add_argument("--onvif-timeout", type=float, default=4.0)
    scan.add_argument("--skip-onvif", action="store_true")
    scan.add_argument("--output", default=str(script_dir / "vendor_camera_inventory.csv"))
    scan.set_defaults(handler=command_scan)

    scan_pcap = subparsers.add_parser("scan-pcap", help="Build inventory from a Wireshark capture of ESC discovery.")
    scan_pcap.add_argument("--pcap", required=True)
    scan_pcap.add_argument("--tshark", default=r"C:\Program Files\Wireshark\tshark.exe")
    scan_pcap.add_argument("--command-timeout", type=float, default=60.0)
    scan_pcap.add_argument("--output", default=str(script_dir / "vendor_camera_inventory_from_pcap.csv"))
    scan_pcap.set_defaults(handler=command_scan_pcap)

    assign = subparsers.add_parser("assign", help="Fill new_ip sequentially from a pool.")
    assign.add_argument("--input", default=str(script_dir / "vendor_camera_inventory.csv"))
    assign.add_argument("--output", default=str(script_dir / "vendor_camera_plan.csv"))
    assign.add_argument("--start-ip", required=True)
    assign.add_argument("--end-ip", required=True)
    assign.add_argument("--mask", default="255.255.252.0")
    assign.add_argument("--gateway", required=True)
    assign.add_argument("--dns", default="10.70.200.5")
    assign.set_defaults(handler=command_assign)

    apply_parser = subparsers.add_parser("apply", help="Execute a reviewed CSV plan through the vendor SDK.")
    apply_parser.add_argument("--input", default=str(script_dir / "vendor_camera_plan.csv"))
    apply_parser.add_argument("--output", default=str(script_dir / "vendor_camera_results.csv"))
    apply_parser.add_argument("--vendor-dir", default=default_vendor_dir(script_dir))
    apply_parser.add_argument("--credential", type=parse_credential)
    apply_parser.add_argument("--username", default="Admin")
    apply_parser.add_argument("--workers", type=int, default=3)
    apply_parser.add_argument("--command-timeout", type=float, default=20.0)
    apply_parser.add_argument("--verify-seconds", type=float, default=30.0)
    apply_parser.add_argument("--allow-occupied", action="store_true")
    apply_parser.add_argument("--apply", action="store_true", help="Actually send commands; otherwise dry-run.")
    apply_parser.set_defaults(handler=command_apply)

    audit = subparsers.add_parser("audit-time", help="Read current NTP and timezone settings without writing.")
    audit.add_argument("--input", default=str(script_dir / "vendor_camera_inventory.csv"))
    audit.add_argument("--output", default=str(script_dir / "camera_time_audit.csv"))
    audit.add_argument("--credential", action="append", type=parse_credential, default=[])
    audit.add_argument(
        "--credential-for",
        action="append",
        default=[],
        metavar="START-END=USER:PASSWORD",
        help="Use only this credential for an IP or inclusive IP range.",
    )
    audit.add_argument("--username", default="Admin")
    audit.add_argument("--password", default=None)
    audit.add_argument("--onvif-path", default="/onvif/device_service")
    audit.add_argument("--workers", type=int, default=8)
    audit.add_argument("--http-timeout", type=float, default=5.0)
    audit.add_argument("--ping-timeout-ms", type=int, default=700)
    audit.add_argument("--skip-ping", action="store_true")
    audit.set_defaults(handler=command_audit_time)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
