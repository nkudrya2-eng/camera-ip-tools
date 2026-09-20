import base64
import re
import json
import time
import sys
import yaml
import requests
from urllib.parse import quote
from requests.auth import HTTPDigestAuth
from typing import Optional
from pathlib import Path
from types import SimpleNamespace

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from probe_onvif_time import compact_fault, onvif_post
from sync_camera_time import sync_onvif

CONFIG_FILE  = "camera_config.yaml"
PROFILES_DIR = Path("profiles")


# ============================================================
# ШИФРОВАНИЕ (CROSS)
# ============================================================

def encrypt1(text: str) -> str:
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return "".join(str(127 - ord(ch)).zfill(3) for ch in b64)


# ============================================================
# БАЗОВЫЙ ДРАЙВЕР
# ============================================================

class BaseDriver:
    def __init__(self, profile: dict, cam: dict, cfg: dict):
        self.profile  = profile
        self.cam      = cam
        self.cfg      = cfg
        self.ip       = cam["ip"]
        self.username = cam["username"]
        self.password = cam["password"]

    def connect(self) -> bool:
        raise NotImplementedError

    def apply_network(self)  -> bool: raise NotImplementedError
    def apply_timezone(self) -> bool: raise NotImplementedError
    def apply_ntp(self)      -> bool: raise NotImplementedError
    def apply_motion(self)   -> bool: raise NotImplementedError
    def apply_streams(self)  -> bool: raise NotImplementedError

    def apply(self, flags: dict) -> None:
        print(f"\n{'='*42}")
        print(f"  Камера : {self.ip}")
        print(f"  Профиль: {self.profile.get('driver', '?')}")
        print(f"{'='*42}")

        print("→ Подключение...")
        if not self.connect():
            print("  ✗ Не удалось подключиться")
            return

        actions = {
            "Сеть":              self.apply_network,
            "Часовой пояс":     self.apply_timezone,
            "NTP":              self.apply_ntp,
            "Детектор движения": self.apply_motion,
            "Потоки видео":     self.apply_streams,
        }
        for section, fn in actions.items():
            if flags.get(section):
                print(f"→ {section}...")
                fn()

        print("  Готово.")


# ============================================================
# CROSS DRIVER
# ============================================================

class CrossDriver(BaseDriver):

    def connect(self) -> bool:
        attempts = 2 if self.cam.get("network_autodetect") else 3
        for attempt in range(1, attempts + 1):
            try:
                resp = requests.post(
                    f"http://{self.ip}/action/WEB_UsrLoginAjaxProc",
                    data={"User": encrypt1(self.username),
                          "Pwd":  encrypt1(self.password),
                          "LanguageParam": "3"},
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                             "Referer": f"http://{self.ip}/asppage/common/login.asp?id=3&ret=1"},
                    timeout=4 if self.cam.get("network_autodetect") else 10,
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                print(f"  Попытка {attempt}/{attempts}: {exc}")
                if attempt < attempts:
                    time.sleep(1)
                continue
            if resp.text.startswith("szPath="):
                for part in resp.text.split("&"):
                    if "key=" in part:
                        self.key = part.split("key=")[1].strip()
                        print(f"  ✓ Session key: {self.key}")
                        self._fetch_device_id()
                        return True
            print(f"  ✗ Авторизация: {resp.text}")
            return False
        return False

    def _fetch_device_id(self):
        self.device_id = ""
        for fetch in [
            lambda: requests.post(
                f"http://{self.ip}/asppage/common/Ajax_changeValue.asp",
                data={"key": self.key, "LanguageId": "3"},
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
                timeout=10,
            ),
            lambda: requests.get(
                f"http://{self.ip}/asppage/common/index.asp",
                params={"key": self.key, "lg": "3"}, timeout=10,
            ),
        ]:
            try:
                m = re.search(r'DeviceID[="\s]+([A-Fa-f0-9]{6,})', fetch().text)
                if m:
                    self.device_id = m.group(1)
                    print(f"  ✓ DeviceID: {self.device_id}")
                    return
            except Exception:
                pass
        print("  [WARN] DeviceID не найден")

    def _send(self, xml_data: str, num: int = 2) -> bool:
        body = f"XMLData={quote(xml_data, safe='')}&LanguageId=3&key={self.key}&Num={num}"
        resp = requests.post(
            f"http://{self.ip}/action/WEB_SetValueByAjax",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                     "Referer": f"http://{self.ip}/asppage/common/index.asp?key={self.key}&lg=3&r=0.1"},
            timeout=10,
        )
        self.network_http_status = resp.status_code
        resp.raise_for_status()
        ok = resp.text.strip().startswith("0")
        print(f"  {'✓' if ok else '✗'} Ответ: {resp.text.strip()[:60]}")
        return ok

    def _wrap(self, config_ids: str, items: str) -> str:
        return (
            f'<MPLDCProtocol ProtocolType="1">'
            f'<MPLDCPDeviceConfig ReturnValue="0" OperateType="2">'
            f'<DeviceInfoEx User="" Password="" IP="{self.ip}" Port="30001" DeviceID="{self.device_id}"/>'
            f'{config_ids}{items}'
            f'</MPLDCPDeviceConfig></MPLDCProtocol>'
        )

    def _build_xml(self, sections: list) -> str:
        config_ids = "".join(f'<DeviceConfigID ID="{s["config_id"]}"/>' for s in sections)
        items = "".join(
            f'<ConfigItem ID="{k}" Value="{v}"/>'
            for s in sections for k, v in s["items"].items()
        )
        return self._wrap(config_ids, items)

    def apply_network(self) -> bool:
        n = self.cfg["network"]
        sections = [
            {
                "config_id": "DeviceConfig_LocalNetwork",
                "items": {
                    "ID_staticIPProtoVer": "IPv4",
                    "ID_staticDHCP":       str(n["dhcp"]).lower(),
                    "ID_staticDhcpIP":     n["ip_address"],
                    "ID_staticIPAddress":  n["ip_address"],
                    "ID_staticSubnetmask": n["subnet_mask"],
                    "ID_staticGateway":    n["gateway"],
                    "ID_staticMainDNSIP":  n["dns_main"],
                    "ID_staticSpareDNSIP": n["dns_spare"],
                    "ID_MTU":              str(n["mtu"]),
                    "ID_NetCardId":        "1",
                }
            },
            {"config_id": "DeviceConfig_MTUParam", "items": {}},
        ]
        return self._send(self._build_xml(sections))

    def apply_timezone(self) -> bool:
        t = self.cfg["timezone"]
        sections = [{
            "config_id": "DeviceConfig_TimeZoneParam",
            "items": {
                "ID_Language":      str(t["language"]),
                "ID_TimeZone":      t["timezone"],
                "ID_SDTOpenFlag":   "false",
                "ID_BeginMonth":    "", "ID_BeginWeekly":   "",
                "ID_BeginWeekDays": "0", "ID_BeginTime":    "0",
                "ID_EndMonth":      "", "ID_EndWeekly":     "",
                "ID_EndWeekDays":   "0", "ID_EndTime":      "0",
            }
        }]
        return self._send(self._build_xml(sections))

    def apply_ntp(self) -> bool:
        n = self.cfg["ntp"]
        sections = [{
            "config_id": "DeviceConfig_NTPService",
            "items": {
                "ID_IP":              n["server"],
                "ID_Port":            str(n["port"]),
                "ID_NTPService_Flag": str(n["enabled"]).lower(),
                "ID_IPProtoVer":      "1",
                "ID_TimeCheck":       str(n["interval"]),
            }
        }]
        return self._send(self._build_xml(sections))

    def apply_motion(self) -> bool:
        m = self.cfg["motion"]
        grid = m["grid_data"] or "1" * (m["grid_width"] * m["grid_height"])
        schedule = m.get("schedule") or [(d, 0, 86400) for d in range(7)]
        schedule_str = "".join(
            f"<ScheduleTime WeekDay='{d}' StartTime='{s}' EndTime='{e}'/>"
            for d, s, e in schedule
        )
        schedule_value = f"<ID_ScheduleTime>{schedule_str}</ID_ScheduleTime>"

        config_ids = '<DeviceConfigID ID="DeviceConfig_MotionDetectionAlarm"/>'
        items = (
            f'<ConfigItem ID="ID_CameraId" Value="{m["camera_id"]}"/>'
            f'<ConfigItem ID="ID_AlarmEnableFlag" Value="{str(m["enabled"]).lower()}"/>'
            f'<ConfigItem ID="ID_ScheduleTime" Value="{schedule_value}"/>'
            f'<ConfigItem ID="ID_AlarmOutAction" Value="{m["alarm_out_action"]}"/>'
            f'<ConfigItem ID="ID_Sensitivity" Value="{m["sensitivity"]}"/>'
            f'<ConfigItem ID="ID_WidthCellNumber" Value="{m["grid_width"]}"/>'
            f'<ConfigItem ID="ID_HeightCellNumber" Value="{m["grid_height"]}"/>'
            f'<ConfigItem ID="ID_Data" Value="{grid}"/>'
            f'<ConfigItem ID="ID_AlarmInterval" Value="{m["alarm_interval"]}"/>'
            f'<ConfigItem ID="ID_AlarmRecordAction" Value="{str(m["record"]).lower()}"><Item Value="1"/></ConfigItem>'
            f'<ConfigItem ID="ID_SMTPAction" Value="{str(m["smtp"]).lower()}"/>'
            f'<ConfigItem ID="ID_FTPAction" Value="{str(m["ftp"]).lower()}"/>'
            f'<ConfigItem ID="ID_MotionStreamFlag" Value="{str(m["stream_flag"]).lower()}"/>'
            f'<ConfigItem ID="ID_AudioActionFlag" Value="{str(m["audio_flag"]).lower()}"/>'
            f'<ConfigItem ID="ID_AudioAction" Value="{m["audio_action"]}"/>'
        )
        return self._send(self._wrap(config_ids, items))

    def apply_streams(self) -> bool:
        # Каждый поток — отдельный запрос с Num=3
        ok = True
        for s in self.cfg.get("streams", []):
            config_ids = '<DeviceConfigID ID="DeviceConfig_StreamConfig"/>'
            items = (
                f'<ConfigItem ID="ID_CameraId" Value="{s.get("camera_id", 1)}"/>'
                f'<ConfigItem ID="ID_StreamId" Value="{s["id"]}"/>'
                f'<ConfigItem ID="ID_StreamName" Value="{s["name"]}"/>'
                f'<ConfigItem ID="ID_VideoEncode" Value="{s["codec"]}"/>'
                f'<ConfigItem ID="ID_AudioEncode" Value="{s.get("audio_codec", "G711_ALAW")}"/>'
                f'<ConfigItem ID="ID_Resolution" Value="{s["resolution"]}"/>'
                f'<ConfigItem ID="ID_BiteType" Value="{s["bitrate_type"]}"/>'
                f'<ConfigItem ID="ID_Frame" Value="{s["fps"]}"/>'
                f'<ConfigItem ID="ID_IFrameInterval" Value="{s["gop"]}"/>'
                f'<ConfigItem ID="ID_IFrameInterval_Type" Value="{s.get("gop_interval_type", 2)}"/>'
                f'<ConfigItem ID="ID_Bite" Value="{s["bitrate"]}"/>'
                f'<ConfigItem ID="ID_Quilty" Value="{s["quality"]}"/>'
                f'<ConfigItem ID="ID_MaxBite" Value="{s["bitrate"]}"/>'
                f'<ConfigItem ID="ID_VideoEncodeLevel" Value="{s.get("video_level", 1)}"/>'
                f'<ConfigItem ID="Smart_Enc" Value="{str(s.get("smart_encode", False)).lower()}"/>'
            )
            print(f"  Поток {s['id']} ({s['resolution']})...", end=" ")
            ok &= self._send(self._wrap(config_ids, items), num=3)
        return ok


# ============================================================
# APIX E8 DRIVER  (LAPI/V1.0, PUT JSON, HTTP Digest MD5)
# ============================================================

class ApixE8Driver(BaseDriver):

    def connect(self) -> bool:
        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(self.username, self.password)
        self.onvif_fallback = False
        self.onvif_time_result = None
        attempts = 2 if self.cam.get("network_autodetect") else 3
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.get(
                    f"http://{self.ip}/LAPI/V1.0/System/DeviceInfo",
                    timeout=4 if self.cam.get("network_autodetect") else 10,
                )
                if resp.status_code == 200:
                    print("  ✓ LAPI HTTP 200")
                    return True
                print(f"  LAPI HTTP {resp.status_code}")
                break
            except requests.RequestException as exc:
                print(f"  LAPI попытка {attempt}/{attempts}: {exc}")
                if attempt < attempts:
                    time.sleep(1)
        if self.cam.get("network_autodetect"):
            # ONVIF time access does not establish support for LAPI network writes.
            return False
        try:
            root = onvif_post(
                self.ip,
                "/onvif/device_service",
                self.username,
                self.password,
                5.0,
                "GetSystemDateAndTime",
                "<tds:GetSystemDateAndTime/>",
            )
            fault = compact_fault(root)
            if fault:
                raise RuntimeError(fault)
            self.onvif_fallback = True
            print("  ✓ ONVIF")
            return True
        except Exception as exc:
            print(f"  ✗ ONVIF: {exc}")
            return False

    def _apply_onvif_time(self) -> bool:
        if self.onvif_time_result is not None:
            return self.onvif_time_result
        args = SimpleNamespace(
            dry_run=False,
            ntp_server=self.cfg["ntp"]["server"],
            from_dhcp=False,
            timezone=self.cfg["timezone"].get("timezone_offset", "+00:00"),
            timezone_onvif=self.cfg["timezone"].get("timezone_onvif", ""),
            daylight_savings=False,
            onvif_path="/onvif/device_service",
            http_timeout=5.0,
        )
        ok, message = sync_onvif(self.ip, self.username, self.password, args)
        print(f"  {'✓' if ok else '✗'} ONVIF: {message}")
        self.onvif_time_result = ok
        return ok

    def _request(self, section: str, body: dict) -> bool:
        ep = self.profile["endpoints"][section]
        resp = self.session.request(
            ep["method"],
            f"http://{self.ip}{ep['url']}",
            json=body,
            headers={"Content-Type": "application/json; charset=UTF-8",
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=10,
        )
        ok = resp.status_code in (200, 204)
        self.network_http_status = resp.status_code
        if section == "network":
            print(f"  Ответ настройки сети: {resp.text[:800]}")
            if resp.text.lstrip().lower().startswith(("<!doctype html", "<html")):
                print("  Вместо ответа API получена HTML-страница; смена IP не подтверждена")
                return False
        print(f"  {'✓' if ok else '✗'} HTTP {resp.status_code}"
              + (f": {resp.text[:60]}" if not ok else ""))
        return ok

    def apply_network(self) -> bool:
        if self.onvif_fallback:
            print("  ✗ Смена сети через ONVIF для этого профиля не реализована")
            return False
        n = self.cfg["network"]

        # Вариант 1: Подтверждённый LAPI формат (/LAPI/V1.0/Network/Interfaces)
        payload_interfaces = {
            "Num": 1,
            "NetworkInterfaceList": [
                {
                    "ID": 1,
                    "Name": "eth0",
                    "WorkMode": 0,
                    "IsInnerNIC": 1,
                    "InnerNICIPAddress": n["ip_address"],
                    "InnerNICNetmask": n["subnet_mask"],
                    "InnerNICName": "eth0",
                    "MTU": n.get("mtu", 1500),
                    "MAC": "",
                    "NegotiationMode": 0,
                    "IPv4": {
                        "IPGetType": 1 if n.get("dhcp") else 0,
                        "PPPoE": {"LoginName": "", "PIN": ""},
                        "AddressNum": 1,
                        "AddressList": [
                            {
                                "Address": n["ip_address"],
                                "Netmask": n["subnet_mask"],
                                "Gateway": n["gateway"],
                            }
                        ],
                    },
                    "IPv6": {
                        "IPGetType": 1,
                        "AddressNum": 1,
                        "AddressList": [{"PrefixLenth": 64, "Address": "", "Gateway": ""}],
                    },
                }
            ],
            "DefaultRouteNIC": 1,
            "WorkMode": 0,
        }

        try:
            resp = self.session.put(
                f"http://{self.ip}/LAPI/V1.0/Network/Interfaces",
                json=payload_interfaces,
                headers={"Content-Type": "application/json; charset=UTF-8", "X-Requested-With": "XMLHttpRequest"},
                timeout=10,
            )
            self.network_http_status = resp.status_code
            print(f"  LAPI Network/Interfaces: HTTP {resp.status_code}")
            if resp.status_code in (200, 204):
                if not resp.text.lstrip().lower().startswith(("<!doctype html", "<html")):
                    print(f"  ✓ Сеть успешно обновлена через /LAPI/V1.0/Network/Interfaces: {resp.text[:200]}")
                    return True
        except requests.RequestException as exc:
            print(f"  Ошибка /LAPI/V1.0/Network/Interfaces: {exc}")

        # Вариант 2: Альтернативный формат Uniview LAPI (/LAPI/V1.0/Interfaces/0/Network)
        payload_single = {
            "ID": 1,
            "Name": "eth0",
            "MTU": n.get("mtu", 1500),
            "IPv4": {
                "IPGetType": 1 if n.get("dhcp") else 0,
                "AddressNum": 1,
                "AddressList": [
                    {
                        "Address": n["ip_address"],
                        "Netmask": n["subnet_mask"],
                        "Gateway": n["gateway"],
                    }
                ],
            },
        }

        try:
            resp = self.session.put(
                f"http://{self.ip}/LAPI/V1.0/Interfaces/0/Network",
                json=payload_single,
                headers={"Content-Type": "application/json; charset=UTF-8", "X-Requested-With": "XMLHttpRequest"},
                timeout=10,
            )
            self.network_http_status = resp.status_code
            print(f"  LAPI Interfaces/0/Network: HTTP {resp.status_code}")
            if resp.status_code in (200, 204):
                if not resp.text.lstrip().lower().startswith(("<!doctype html", "<html")):
                    print(f"  ✓ Сеть успешно обновлена через /LAPI/V1.0/Interfaces/0/Network: {resp.text[:200]}")
                    return True
        except requests.RequestException as exc:
            print(f"  Ошибка /LAPI/V1.0/Interfaces/0/Network: {exc}")

        return False

    def apply_timezone(self) -> bool:
        if self.onvif_fallback:
            return self._apply_onvif_time()
        timezone = self.cfg["timezone"].get("timezone_lapi")
        if not timezone:
            match = re.search(r"GMT([+-]\d{2}:\d{2})", self.cfg["timezone"].get("timezone", ""))
            timezone = f"GMT{match.group(1)}" if match else "GMT+00:00"
        return self._request("timezone", {
            "TimeZone": timezone,
        })

    def apply_ntp(self) -> bool:
        if self.onvif_fallback:
            return self._apply_onvif_time()
        n = self.cfg["ntp"]
        return self._request("ntp", {
            "Num": 1,
            "NTPServerInfos": [{
                "Enabled":             1 if n["enabled"] else 0,
                "AddressType":         0,
                "IPAddress":           n["server"],
                "Port":                n["port"],
                "SynchronizeInterval": n["interval"],
            }],
        })

    def apply_motion(self) -> bool:
        if self.onvif_fallback:
            print("  ✗ Детектор движения через ONVIF для этого профиля не реализован")
            return False
        m = self.cfg["motion"]
        return self._request("motion", {
            "Enabled": 1 if m["enabled"] else 0,
            "Mode":    m.get("motion_mode", 0),
        })

    def apply_streams(self) -> bool:
        if self.onvif_fallback:
            print("  ✗ Потоки через ONVIF для этого профиля не реализованы")
            return False
        # E8 принимает все потоки одним запросом
        streams = self.cfg.get("streams", [])
        infos = []
        for s in streams:
            w, h = s["resolution"].split("x")
            # codec → EncodeFormat: H264=1, H265=2
            encode_fmt = 1 if s["codec"].upper() == "H264" else 2
            # bitrate_type → BitRateType: CBR=0, VBR=1
            brt = 0 if s["bitrate_type"].upper() == "CBR" else 1
            infos.append({
                "ID":      s["id"] - 1,   # E8 нумерует с 0
                "Enabled": 1 if s.get("enabled", True) else 0,
                "VideoEncodeInfo": {
                    "EncodeFormat":  encode_fmt,
                    "Resolution":    {"Width": int(w), "Height": int(h)},
                    "BitRate":       s["bitrate"],
                    "BitRateType":   brt,
                    "FrameRate":     s["fps"],
                    "GOPType":       s.get("gop_type", 0),
                    "IFrameInterval":s["gop"],
                    "ImageQuality":  s["quality"],
                    "SmoothLevel":   s.get("smooth_level", 5),
                    "SVCMode":       0,
                    "SmartEncodeMode": 1 if s.get("smart_encode", False) else 0,
                },
            })
        ep = self.profile["endpoints"]["streams"]
        resp = self.session.request(
            ep["method"],
            f"http://{self.ip}{ep['url']}",
            json={"Num": len(infos), "VideoStreamInfos": infos},
            headers={"Content-Type": "application/json; charset=UTF-8",
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=10,
        )
        ok = resp.status_code in (200, 204)
        print(f"  {'✓' if ok else '✗'} HTTP {resp.status_code}"
              + (f": {resp.text[:60]}" if not ok else ""))
        return ok


# ============================================================
# APIX S8 DRIVER  (CGI, POST JSON, HTTP Digest)
# ============================================================

class ApixS8Driver(BaseDriver):

    def connect(self) -> bool:
        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(self.username, self.password)
        self.session.headers.update({"x-from": "Web", "User-Agent": "Mozilla/5.0"})
        self.onvif_fallback = False
        self.onvif_time_result = None
        attempts = 2 if self.cam.get("network_autodetect") else 3
        for attempt in range(1, attempts + 1):
            try:
                # Авторизация через /vb.htm (инициализирует Digest Auth в веб-сервере S8)
                resp = self.session.get(
                    f"http://{self.ip}/vb.htm?language=ie&curmaxconn",
                    timeout=4 if self.cam.get("network_autodetect") else 10,
                )
                if resp.status_code == 200:
                    print("  ✓ S8 Auth HTTP 200 (vb.htm)")
                    return True
                if resp.status_code == 401:
                    print("  ✗ S8 Auth 401 (неверный логин/пароль)")
                    return False
            except requests.RequestException as e:
                print(f"  S8 попытка {attempt}/{attempts}: {e}")
                if attempt < attempts:
                    time.sleep(1)

        # Резервная проверка через get.network.tcp
        try:
            resp = self.session.get(
                f"http://{self.ip}/cgi-bin/admin/admin.cgi?action=get.network.tcp&format=json",
                timeout=4 if self.cam.get("network_autodetect") else 10,
            )
            if resp.status_code == 200 and "ipv4" in resp.text.lower():
                print("  ✓ S8 admin.cgi HTTP 200")
                return True
        except Exception:
            pass

        if self.cam.get("network_autodetect"):
            return False
        try:
            root = onvif_post(
                self.ip,
                "/onvif/device_service",
                self.username,
                self.password,
                5.0,
                "GetSystemDateAndTime",
                "<tds:GetSystemDateAndTime/>",
            )
            fault = compact_fault(root)
            if fault:
                raise RuntimeError(fault)
            self.onvif_fallback = True
            print("  ✓ ONVIF")
            return True
        except Exception as e:
            print(f"  ✗ ONVIF: {e}")
            return False

    def _apply_onvif_time(self) -> bool:
        if self.onvif_time_result is not None:
            return self.onvif_time_result

        args = SimpleNamespace(
            dry_run=False,
            ntp_server=self.cfg["ntp"]["server"],
            from_dhcp=False,
            timezone=self.cfg["timezone"].get("timezone_offset", "+00:00"),
            timezone_onvif=self.cfg["timezone"].get("timezone_onvif", ""),
            daylight_savings=False,
            onvif_path="/onvif/device_service",
            http_timeout=5.0,
        )
        ok, message = sync_onvif(self.ip, self.username, self.password, args)
        print(f"  {'✓' if ok else '✗'} ONVIF: {message}")
        self.onvif_time_result = ok
        return ok

    def _request(self, section: str, body: dict) -> bool:
        ep = self.profile["endpoints"][section]
        resp = self.session.request(
            ep["method"],
            f"http://{self.ip}{ep['url']}",
            json=body,
            headers={"Content-Type": "application/json",
                     "x-from": "Web"},
            timeout=10,
        )
        ok = resp.status_code in (200, 204)
        self.network_http_status = resp.status_code
        text_lower = resp.text.lower()
        if "error" in text_lower or "return=-" in text_lower or "fail" in text_lower:
            ok = False
        if "succeed" in text_lower or "success" in text_lower:
            ok = True
        if section == "network":
            print(f"  Ответ настройки сети: {resp.text[:800]}")
            if resp.text.lstrip().lower().startswith(("<!doctype html", "<html")):
                print("  Вместо ответа API получена HTML-страница; смена IP не подтверждена")
                return False
            if not ok:
                print("  API вернул код ошибки; смена IP не подтверждена")
                return False
        print(f"  {'✓' if ok else '✗'} HTTP {resp.status_code}"
              + (f": {resp.text[:60]}" if not ok else ""))
        return ok

    def apply_network(self) -> bool:
        n = self.cfg["network"]
        return self._request("network", {
            "mtuByte":        int(n.get("mtu", 1500)),
            "ipv4Ipaddress":  n["ip_address"],
            "ipv4DhcpEnable": 1 if n.get("dhcp") else 0,
            "ipv4netmask":    n["subnet_mask"],
            "ipv4gateway":    n["gateway"],
            "dns0":           n.get("dns_main", "8.8.8.8"),
            "ipv6Mode":       0,
            "ipv6Ipaddress":  "",
            "ipv6netmask":    0,
            "ipv6gateway":    "",
        })

    def _apply_time(self) -> bool:
        """S8 объединяет таймзону и NTP в одном запросе"""
        t = self.cfg["timezone"]
        n = self.cfg["ntp"]
        tz_utc = t.get("timezone_utc", "")
        if not tz_utc:
            match = re.search(r"GMT([+-])(\d{2}):(\d{2})", t.get("timezone", ""))
            if match:
                sign, hours, minutes = match.groups()
                posix_sign = "-" if sign == "+" else "+"
                tz_min = f":{minutes}" if minutes != "00" else ""
                tz_utc = f"UTC{posix_sign}{int(hours)}{tz_min}"
            else:
                tz_utc = "UTC-9"
        elif tz_utc.startswith("UTC+"):
            # POSIX-инверсия: для восточного полушария (Россия) пишется минус (UTC-9 для GMT+9)
            tz_utc = "UTC-" + tz_utc[4:]
        zone_name = t.get("timezone_name") or "RUS"
        return self._request("time", {
            "timeZoneTz":    tz_utc,
            "zoneNameTz":    zone_name,
            "dayLight":      0,
            "timeType":      0,
            "ntpServer":     n.get("server", "pool.ntp.org"),
            "ntpSyncEnable": 1 if n.get("enabled", True) else 0,
            "ntpInterval":   max(1, n.get("interval", 3600) // 60),
        })

    def apply_timezone(self) -> bool:
        if self.onvif_fallback:
            return self._apply_onvif_time()
        return self._apply_time()

    def apply_ntp(self) -> bool:
        if self.onvif_fallback:
            return self._apply_onvif_time()
        # Уже отправлено в apply_timezone если оба флага стоят,
        # но на случай если только NTP — шлём снова
        return self._apply_time()

    def apply_motion(self) -> bool:
        m = self.cfg["motion"]
        grid = m["grid_data"] or "1" * 300
        schedule = m.get("schedule") or [
            {"scheduleIndex": d, "schedule": "00:00:24:00;"}
            for d in range(7)
        ]
        if schedule and isinstance(schedule[0], (list, tuple)):
            schedule = [
                {"scheduleIndex": d, "schedule": "00:00:24:00;"}
                for d in range(7)
            ]
        return self._request("motion", {
            "motionEnable":          1 if m["enabled"] else 0,
            "dynamicAnalysisEnable": 0,
            "detectionMode":         m.get("motion_mode", 0),
            "triggerSensitivity":    m["sensitivity"],
            "activecell":            0,
            "windowRectangle":       grid,
            "scheduleList":          schedule,
        })

    def apply_streams(self) -> bool:
        # S8 настраивает потоки через streamList
        streams = self.cfg.get("streams", [])
        stream_list = {}
        stream_names = ["mainStream", "subStream", "thirdStream"]
        for s in streams:
            idx = s["id"] - 1   # id 1→mainStream, 2→subStream, 3→thirdStream
            if idx >= len(stream_names):
                continue
            w, h = s["resolution"].split("x")
            # codec → profileCodec: H264=0, H265=1
            codec = 0 if s["codec"].upper() == "H264" else 1
            # bitrate_type → rateMode: CBR=0, VBR=1
            rate_mode = 0 if s["bitrate_type"].upper() == "CBR" else 1
            stream_list[stream_names[idx]] = {
                "width":            int(w),
                "height":           int(h),
                "profile":          1,
                "profileCodec":     codec,
                "profileGop":       s["gop"],
                "framerate":        s["fps"],
                "bitrate":          s["bitrate"],
                "vbrQuality":       s.get("quality", 1),
                "smartStreamLevel": s.get("smooth_level", 5),
                "rateMode":         rate_mode,
                "rateQuality":      50,
                "smartStreamEnable":1 if s.get("smart_encode", False) else 0,
            }
        return self._request("streams", {"streamList": stream_list})


# ============================================================
# ФАБРИКА ДРАЙВЕРОВ
# ============================================================

DRIVERS = {
    "cross":    CrossDriver,
    "apix_e8":  ApixE8Driver,
    "apix_s8":  ApixS8Driver,
}


def load_profile(name: str) -> dict:
    path = PROFILES_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Профиль не найден: {path}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def configure_camera(cam: dict, cfg: dict, flags: dict) -> bool:
    profile_name = cam.get("profile", "cross")
    try:
        profile = load_profile(profile_name)
    except FileNotFoundError as e:
        print(f"  [ERROR] {e}")
        return False

    driver_cls = DRIVERS.get(profile["driver"])
    if not driver_cls:
        print(f"  [ERROR] Неизвестный драйвер: {profile['driver']}")
        return False

    driver = driver_cls(profile, cam, cfg)
    driver.apply(flags)
    return True


# ============================================================
# ПАРСЕР ПУЛА IP
# ============================================================

def expand_cameras(cameras: list) -> list:
    result = []
    for cam in cameras:
        m = re.match(r'^(\d+\.\d+\.\d+\.)(\d+)-(\d+)$', str(cam.get("ip", "")).strip())
        if m:
            prefix, start, end = m.group(1), int(m.group(2)), int(m.group(3))
            for i in range(start, end + 1):
                result.append({**cam, "ip": f"{prefix}{i}"})
        else:
            result.append(cam)
    return result


# ============================================================
# КОНСОЛЬНОЕ МЕНЮ (стрелки + пробел, работает на Windows)
# ============================================================

SECTIONS = ["Сеть", "Часовой пояс", "NTP", "Детектор движения", "Потоки видео"]


def console_menu() -> dict:
    import sys
    import os

    flags   = [True] * len(SECTIONS)
    current = 0

    def clear_menu(lines: int):
        for _ in range(lines):
            sys.stdout.write("\033[F\033[2K")
        sys.stdout.flush()

    def draw_menu():
        print("─" * 44)
        print("  Разделы для применения:")
        print("  ↑↓ — навигация  |  Пробел — вкл/выкл  |  Enter — применить")
        print("─" * 44)
        for i, name in enumerate(SECTIONS):
            cursor = "▶" if i == current else " "
            check  = "●" if flags[i] else "○"
            print(f"  {cursor} {check}  {name}")
        print("─" * 44)
        sys.stdout.flush()

    MENU_LINES = len(SECTIONS) + 5

    # Первый рендер
    draw_menu()

    if os.name == "nt":
        import msvcrt
        def get_key():
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):   # спецклавиши
                key2 = msvcrt.getwch()
                return {"H": "UP", "P": "DOWN"}.get(key2, "")
            if key == "\r":  return "ENTER"
            if key == " ":   return "SPACE"
            if key == "\x03": raise KeyboardInterrupt
            return ""
    else:
        import tty, termios
        def get_key():
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    ch2 = sys.stdin.read(2)
                    return {"[A": "UP", "[B": "DOWN"}.get(ch2, "")
                if ch == "\r" or ch == "\n": return "ENTER"
                if ch == " ":               return "SPACE"
                if ch == "\x03":            raise KeyboardInterrupt
                return ""
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    try:
        while True:
            key = get_key()
            if not key:
                continue

            clear_menu(MENU_LINES)

            if key == "UP":
                current = (current - 1) % len(SECTIONS)
            elif key == "DOWN":
                current = (current + 1) % len(SECTIONS)
            elif key == "SPACE":
                flags[current] = not flags[current]
            elif key == "ENTER":
                if not any(flags):
                    pass  # перерисуем без изменений
                else:
                    draw_menu()
                    print()
                    return {name: flags[i] for i, name in enumerate(SECTIONS)}

            draw_menu()

    except KeyboardInterrupt:
        print("\nОтмена.")
        exit(0)



# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"[ERROR] Файл конфига не найден: {CONFIG_FILE}")
        exit(1)

    cameras = expand_cameras(cfg.get("cameras") or [cfg.get("camera")])
    flags   = console_menu()

    print(f"\nКамер для настройки: {len(cameras)}")
    ok_count = 0
    for i, cam in enumerate(cameras, 1):
        print(f"\n[{i}/{len(cameras)}]", end="")
        try:
            configure_camera(cam, cfg, flags)
            ok_count += 1
        except Exception as e:
            print(f"\n  [ERROR] {cam['ip']}: {e}")

    print(f"\n{'='*42}")
    print(f"  Итог: {ok_count}/{len(cameras)} камер обработано")
    print(f"{'='*42}")
