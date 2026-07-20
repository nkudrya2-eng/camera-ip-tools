import ipaddress
import json
import re
from types import SimpleNamespace
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth

from audit_camera_time import audit_one, parse_sunell_values, sunell_get
from probe_onvif_time import NS, onvif_post, wsse_header


LAPI_STREAM_PATHS = (
    "/LAPI/V1.0/Channel/0/Media/Video/Streams/DetailInfos",
    "/LAPI/V1.0/Media/Video/Streams",
    "/LAPI/V1.0/Video/Streams",
)
LAPI_NETWORK_PATHS = (
    "/LAPI/V1.0/Network/Interfaces",
    "/LAPI/V1.0/Interfaces/0/Network",
)


def probe_onvif_camera(ip: str, username: str, password: str, timeout: float = 4.0) -> dict | None:
    """Confirm an address is an ONVIF camera and return a discovery-compatible row."""
    try:
        device = onvif_post(
            ip,
            "/onvif/device_service",
            username,
            password,
            timeout,
            "GetDeviceInformation",
            "<tds:GetDeviceInformation/>",
        )
    except Exception:
        return None

    attributes = {}
    for element in device.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name in {"Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId"}:
            attributes[name] = (element.text or "").strip()

    mac = ""
    mask = ""
    try:
        interfaces = onvif_post(
            ip,
            "/onvif/device_service",
            username,
            password,
            timeout,
            "GetNetworkInterfaces",
            "<tds:GetNetworkInterfaces/>",
        )
        prefix_length = ""
        for element in interfaces.iter():
            name = element.tag.rsplit("}", 1)[-1]
            text = (element.text or "").strip()
            if name == "HwAddress" and text and not mac:
                mac = text
            elif name == "PrefixLength" and text.isdigit() and not prefix_length:
                prefix_length = text
        if prefix_length:
            mask = str(ipaddress.IPv4Network(f"0.0.0.0/{prefix_length}").netmask)
    except Exception:
        pass

    return {
        "selected": "1",
        "protocol": "onvif",
        "current_ip": ip,
        "new_ip": "",
        "model": attributes.get("Model", ""),
        "mac": mac,
        "device_id": "",
        "serial_number": attributes.get("SerialNumber", ""),
        "device_name": "",
        "manufacturer": attributes.get("Manufacturer", ""),
        "firmware": attributes.get("FirmwareVersion", ""),
        "http_port": "80",
        "mask": mask,
        "gateway": "",
        "dns": "",
        "status": "discovered",
        "message": "direct ONVIF probe",
        "raw_attributes": json.dumps(attributes, ensure_ascii=False, separators=(",", ":")),
    }


def _find_first(value, names: set[str]):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in names and item not in ("", None, "null"):
                return str(item)
        for item in value.values():
            found = _find_first(item, names)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first(item, names)
            if found:
                return found
    return ""


def _parse_key_values(text: str) -> dict[str, str]:
    values = parse_sunell_values(text)
    if values:
        return values
    result = {}
    for line in text.replace(";", "\n").replace("&", "\n").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _read_network(ip: str, username: str, password: str, timeout: float) -> dict:
    session = requests.Session()
    session.auth = HTTPDigestAuth(username, password)
    for path in LAPI_NETWORK_PATHS:
        try:
            response = session.get(f"http://{ip}{path}", timeout=timeout)
            if response.status_code == 200:
                data = response.json()
                mask = _find_first(data, {"netmask", "subnetmask", "subnet"})
                gateway = _find_first(data, {"gateway", "defaultgateway"})
                if mask or gateway:
                    return {"mask": mask, "gateway_read": gateway, "network_source": "lapi"}
        except (requests.RequestException, ValueError):
            pass

    try:
        response = session.get(
            f"http://{ip}/cgi-bin/operator/operator.cgi?action=get.network.eth0&format=json",
            timeout=timeout,
        )
        if response.status_code == 200:
            data = response.json()
            mask = _find_first(data, {"ipv4netmask", "netmask", "subnetmask"})
            gateway = _find_first(data, {"ipv4gateway", "gateway"})
            if mask or gateway:
                return {"mask": mask, "gateway_read": gateway, "network_source": "s8_cgi"}
    except (requests.RequestException, ValueError):
        pass

    try:
        response = requests.get(
            f"http://{ip}/cgi-bin/param.cgi",
            params={
                "userName": username,
                "password": password,
                "action": "get",
                "type": "localNetwork",
                "IPProtoVer": "1",
                "netCardId": "1",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        values = {key.lower(): value for key, value in _parse_key_values(response.text).items()}
        mask = values.get("subnetmask", "") or values.get("netmask", "")
        gateway = values.get("subgetway", "") or values.get("gateway", "")
        if mask or gateway:
            return {"mask": mask, "gateway_read": gateway, "network_source": "sunell"}
    except requests.RequestException:
        pass
    return {"mask": "", "gateway_read": "", "network_source": ""}


def _read_device(ip: str, username: str, password: str, timeout: float) -> dict:
    session = requests.Session()
    session.auth = HTTPDigestAuth(username, password)
    for path in ("/LAPI/V1.0/System/DeviceInfo", "/cgi-bin/operator/operator.cgi?action=get.device.info&format=json"):
        try:
            response = session.get(f"http://{ip}{path}", timeout=timeout)
            if response.status_code != 200:
                continue
            data = response.json()
            serial = _find_first(data, {"serialnumber", "serialno", "serial", "sn"})
            if serial:
                return {"serial_number": serial, "device_source": "lapi_cgi"}
        except (requests.RequestException, ValueError):
            pass
    try:
        root = onvif_post(
            ip,
            "/onvif/device_service",
            username,
            password,
            timeout,
            "GetDeviceInformation",
            "<tds:GetDeviceInformation/>",
        )
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == "SerialNumber" and element.text:
                return {"serial_number": element.text.strip(), "device_source": "onvif"}
    except Exception:
        pass
    return {"serial_number": "", "device_source": ""}


def _codec_from_value(value) -> str:
    if isinstance(value, str):
        normalized = value.upper().replace(".", "")
        if "H265" in normalized or "HEVC" in normalized:
            return "H.265"
        if "H264" in normalized or "AVC" in normalized:
            return "H.264"
    if isinstance(value, int):
        return {1: "H.264", 2: "H.265"}.get(value, "")
    return ""


def _find_codec(value) -> str:
    if isinstance(value, dict):
        preferred = ("EncodeFormat", "VideoEncode", "Codec", "Encoding", "videoCodec")
        for key in preferred:
            if key in value:
                codec = _codec_from_value(value[key])
                if codec:
                    return codec
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                codec = _find_codec(nested)
                if codec:
                    return codec
    elif isinstance(value, list):
        for nested in value:
            codec = _find_codec(nested)
            if codec:
                return codec
    elif isinstance(value, str):
        return _codec_from_value(value)
    return ""


def _read_http_codec(ip: str, username: str, password: str, timeout: float) -> str:
    session = requests.Session()
    session.auth = HTTPDigestAuth(username, password)
    for path in LAPI_STREAM_PATHS:
        try:
            response = session.get(f"http://{ip}{path}", timeout=timeout)
            if response.status_code == 200:
                codec = _find_codec(response.json())
                if codec:
                    return codec
        except (requests.RequestException, ValueError):
            pass

    try:
        response = session.get(
            f"http://{ip}/cgi-bin/operator/operator.cgi?action=get.video.general&format=json",
            timeout=timeout,
        )
        if response.status_code == 200:
            codec = _find_codec(response.json())
            if codec:
                return codec
    except (requests.RequestException, ValueError):
        pass

    for info_type in ("videoEncode", "streamConfig", "video"):
        try:
            text = sunell_get(ip, username, password, timeout, info_type)
        except Exception:
            continue
        values = parse_sunell_values(text)
        codec = _find_codec(values)
        if not codec:
            match = re.search(r"(?:VideoEncode|Codec|Encoding)\s*[=:]\s*([^\s;&]+)", text, re.I)
            codec = _codec_from_value(match.group(1)) if match else ""
        if codec:
            return codec
    return _read_onvif_codec(ip, username, password, timeout)


def _read_onvif_codec(ip: str, username: str, password: str, timeout: float) -> str:
    try:
        capabilities = onvif_post(
            ip,
            "/onvif/device_service",
            username,
            password,
            timeout,
            "GetCapabilities",
            "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>",
        )
        media_url = ""
        for element in capabilities.iter():
            if element.tag.rsplit("}", 1)[-1] != "Media":
                continue
            for child in element.iter():
                if child.tag.rsplit("}", 1)[-1] == "XAddr" and child.text:
                    media_url = child.text.strip()
                    break
        if not media_url:
            media_url = f"http://{ip}/onvif/Media"
        parsed = urllib.parse.urlsplit(media_url)
        if parsed.hostname not in {ip, None}:
            media_url = urllib.parse.urlunsplit((parsed.scheme or "http", ip, parsed.path, parsed.query, ""))

        header = wsse_header(username, password)
        envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{NS['s']}" xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
 xmlns:tt="{NS['tt']}" xmlns:wsse="{NS['wsse']}" xmlns:wsu="{NS['wsu']}">
  {header}
  <s:Body><trt:GetProfiles/></s:Body>
</s:Envelope>""".encode("utf-8")
        request = urllib.request.Request(
            media_url,
            data=envelope,
            method="POST",
            headers={
                "Content-Type": 'application/soap+xml; charset=utf-8; action="http://www.onvif.org/ver10/media/wsdl/GetProfiles"'
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            root = ET.fromstring(response.read())
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == "Encoding" and element.text:
                codec = _codec_from_value(element.text)
                if codec:
                    return codec
    except Exception:
        pass
    return ""


def read_camera_settings(row: dict, username: str, password: str, timeout: float = 4.0) -> dict:
    vendor = row.get("vendor_row") or {}
    inventory_row = {
        "current_ip": row.get("ip", ""),
        "model": row.get("model", ""),
        "mac": row.get("mac", ""),
        "protocol": vendor.get("protocol", row.get("protocol", "")),
    }
    args = SimpleNamespace(
        skip_ping=True,
        ping_timeout_ms=500,
        onvif_path="/onvif/device_service",
        http_timeout=timeout,
    )
    audit = audit_one(inventory_row, [(username, password)], [], args)
    network = _read_network(row.get("ip", ""), username, password, timeout)
    device = _read_device(row.get("ip", ""), username, password, timeout)
    codec = _read_http_codec(row.get("ip", ""), username, password, timeout)
    has_any = any(
        (
            network.get("mask"), network.get("gateway_read"), audit.get("ntp_server"),
            audit.get("timezone"), device.get("serial_number"), codec,
        )
    )
    details_status = audit.get("status", "failed")
    if details_status == "failed" and has_any:
        details_status = "partial"
    return {
        "mask": network.get("mask") or row.get("mask") or vendor.get("mask", ""),
        "gateway_read": network.get("gateway_read") or row.get("gateway_read") or vendor.get("gateway", ""),
        "ntp_read": audit.get("ntp_server", ""),
        "timezone_read": audit.get("timezone", ""),
        "serial_number": row.get("serial_number") or device.get("serial_number", ""),
        "codec": codec,
        "details_status": details_status,
        "details_source": "+".join(
            filter(None, (audit.get("source", ""), network.get("network_source", ""), device.get("device_source", "")))
        ),
        "details_message": audit.get("message", ""),
    }
