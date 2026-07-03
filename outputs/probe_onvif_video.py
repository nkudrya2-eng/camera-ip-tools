import argparse
import csv
import getpass
import ipaddress
import pathlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from probe_onvif_time import (
    NETWORK_ERRORS,
    NS,
    compact_fault,
    iter_range,
    parse_credential,
    ping,
    soap_envelope,
    text_or_empty,
)


NS_VIDEO = dict(NS)
NS_VIDEO["trt"] = "http://www.onvif.org/ver10/media/wsdl"


def iter_target_ips(args: argparse.Namespace):
    if args.ip_list:
        for raw in args.ip_list.split(","):
            ip = raw.strip()
            if ip:
                ipaddress.IPv4Address(ip)
                yield ip
        return
    yield from iter_range(args.start_ip, args.end_ip)


def post_onvif_url(url: str, username: str, password: str, timeout: float, action_url: str, body_xml: str) -> ET.Element:
    request = urllib.request.Request(
        url,
        data=soap_envelope(body_xml, username, password),
        method="POST",
        headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action_url}"'},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
    return ET.fromstring(text)


def service_url(ip: str, path_or_url: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    return f"http://{ip}{path_or_url}"


def discover_media_urls(ip: str, username: str, password: str, args: argparse.Namespace) -> list[str]:
    urls = []
    device_url = service_url(ip, args.device_path)
    try:
        root = post_onvif_url(
            device_url,
            username,
            password,
            args.http_timeout,
            "http://www.onvif.org/ver10/device/wsdl/GetCapabilities",
            "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>",
        )
        for node in root.findall(".//tt:Media/tt:XAddr", NS_VIDEO):
            if node.text and node.text.strip():
                urls.append(node.text.strip())
    except NETWORK_ERRORS:
        pass

    for path in args.media_paths.split(","):
        path = path.strip()
        if path:
            urls.append(service_url(ip, path))

    seen = set()
    result = []
    for url in urls:
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname and parsed.hostname not in {ip, "0.0.0.0"}:
            url = urllib.parse.urlunparse(parsed._replace(netloc=ip + (f":{parsed.port}" if parsed.port else "")))
        if url not in seen:
            result.append(url)
            seen.add(url)
    return result


def read_profiles(media_url: str, username: str, password: str, args: argparse.Namespace) -> ET.Element:
    return post_onvif_url(
        media_url,
        username,
        password,
        args.http_timeout,
        "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
        "<trt:GetProfiles/>",
    )


def profile_rows(ip: str, media_url: str, root: ET.Element) -> list[dict]:
    rows = []
    for profile in root.findall(".//trt:Profiles", NS_VIDEO):
        encoder = profile.find("tt:VideoEncoderConfiguration", NS_VIDEO)
        row = {
            "ip": ip,
            "ok": "1",
            "media_url": media_url,
            "profile_token": profile.attrib.get("token", ""),
            "profile_name": text_or_empty(profile, "tt:Name"),
            "encoder_token": encoder.attrib.get("token", "") if encoder is not None else "",
            "encoder_name": text_or_empty(encoder, "tt:Name") if encoder is not None else "",
            "encoding": text_or_empty(encoder, "tt:Encoding") if encoder is not None else "",
            "width": text_or_empty(encoder, "tt:Resolution/tt:Width") if encoder is not None else "",
            "height": text_or_empty(encoder, "tt:Resolution/tt:Height") if encoder is not None else "",
            "fps": text_or_empty(encoder, "tt:RateControl/tt:FrameRateLimit") if encoder is not None else "",
            "bitrate": text_or_empty(encoder, "tt:RateControl/tt:BitrateLimit") if encoder is not None else "",
            "message": "profile read OK",
        }
        rows.append(row)
    return rows


def probe_one(ip: str, username: str, password: str, args: argparse.Namespace) -> list[dict]:
    base_row = {
        "ip": ip,
        "ok": "0",
        "media_url": "",
        "profile_token": "",
        "profile_name": "",
        "encoder_token": "",
        "encoder_name": "",
        "encoding": "",
        "width": "",
        "height": "",
        "fps": "",
        "bitrate": "",
        "message": "",
    }
    if not args.skip_ping and not ping(ip, args.ping_timeout_ms):
        row = dict(base_row)
        row["message"] = "no ping reply"
        return [row]

    errors = []
    for media_url in discover_media_urls(ip, username, password, args):
        try:
            root = read_profiles(media_url, username, password, args)
            fault = compact_fault(root)
            if fault:
                errors.append(f"{media_url}: {fault}")
                continue
            rows = profile_rows(ip, media_url, root)
            if rows:
                return rows
            errors.append(f"{media_url}: no profiles")
        except NETWORK_ERRORS as exc:
            errors.append(f"{media_url}: {exc}")
        except Exception as exc:
            errors.append(f"{media_url}: {type(exc).__name__}: {exc}")

    row = dict(base_row)
    row["message"] = "; ".join(errors)[:1000] if errors else "media service not found"
    return [row]


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Read ONVIF media profiles and current video codec.")
    parser.add_argument("--start-ip", default="10.53.240.136")
    parser.add_argument("--end-ip", default="10.53.240.136")
    parser.add_argument("--ip-list", default="", help="Comma-separated IP list; overrides --start-ip/--end-ip.")
    parser.add_argument("--credential", type=parse_credential, default=None, help="One credential only: username:password.")
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--device-path", default="/onvif/device_service")
    parser.add_argument("--media-paths", default="/onvif/media_service,/onvif/Media,/onvif/media")
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--skip-ping", action="store_true")
    parser.add_argument("--output-csv", default=str(script_dir / "onvif_video_probe.csv"))
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
        print(f"{ip}: probing ONVIF media")
        current_rows = probe_one(ip, username, password, args)
        ok = any(row["ok"] == "1" for row in current_rows)
        encodings = ", ".join(sorted({row["encoding"] for row in current_rows if row["encoding"]}))
        print(f"{ip}: {'OK' if ok else 'FAILED'} {encodings or current_rows[0]['message']}")
        rows.extend(current_rows)

    output_path = pathlib.Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ip",
        "ok",
        "media_url",
        "profile_token",
        "profile_name",
        "encoder_token",
        "encoder_name",
        "encoding",
        "width",
        "height",
        "fps",
        "bitrate",
        "message",
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    ok_ips = {row["ip"] for row in rows if row["ok"] == "1"}
    print(f"Written {len(rows)} rows to {output_path}")
    print(f"OK cameras: {len(ok_ips)}, failed cameras: {len({row['ip'] for row in rows}) - len(ok_ips)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
