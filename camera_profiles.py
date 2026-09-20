"""Video profiles management and traffic estimation for CameraIpTools."""
import copy
import json
import pathlib
import yaml

DEFAULT_PROFILES = {
    "low": {
        "id": "low",
        "name": "Низкое качество",
        "description": "720p, 10 к/с, H.264, 1.5 Мбит/с",
        "codec": "H264",
        "main": {
            "id": 1,
            "name": "stream1",
            "enabled": True,
            "resolution": "1280x720",
            "fps": 10,
            "codec": "H264",
            "bitrate": 1500,
            "bitrate_type": "CBR",
            "gop": 20,
            "quality": 4,
            "smooth_level": 5,
            "smart_encode": False,
        },
        "sub": {
            "id": 2,
            "name": "stream2",
            "enabled": True,
            "resolution": "640x360",
            "fps": 6,
            "codec": "H264",
            "bitrate": 256,
            "bitrate_type": "CBR",
            "gop": 12,
            "quality": 3,
            "smooth_level": 5,
            "smart_encode": False,
        },
    },
    "medium": {
        "id": "medium",
        "name": "Среднее качество",
        "description": "1080p, 15 к/с, H.264, 3.0 Мбит/с (рекомендуется)",
        "codec": "H264",
        "main": {
            "id": 1,
            "name": "stream1",
            "enabled": True,
            "resolution": "1920x1080",
            "fps": 15,
            "codec": "H264",
            "bitrate": 3000,
            "bitrate_type": "CBR",
            "gop": 30,
            "quality": 5,
            "smooth_level": 5,
            "smart_encode": False,
        },
        "sub": {
            "id": 2,
            "name": "stream2",
            "enabled": True,
            "resolution": "720x576",
            "fps": 10,
            "codec": "H264",
            "bitrate": 512,
            "bitrate_type": "CBR",
            "gop": 20,
            "quality": 4,
            "smooth_level": 5,
            "smart_encode": False,
        },
    },
    "high": {
        "id": "high",
        "name": "Высокое качество",
        "description": "2K / 4Мп, 25 к/с, H.264, 5.0 Мбит/с",
        "codec": "H264",
        "main": {
            "id": 1,
            "name": "stream1",
            "enabled": True,
            "resolution": "2560x1440",
            "fps": 25,
            "codec": "H264",
            "bitrate": 5000,
            "bitrate_type": "CBR",
            "gop": 50,
            "quality": 6,
            "smooth_level": 5,
            "smart_encode": False,
        },
        "sub": {
            "id": 2,
            "name": "stream2",
            "enabled": True,
            "resolution": "1280x720",
            "fps": 15,
            "codec": "H264",
            "bitrate": 1024,
            "bitrate_type": "CBR",
            "gop": 30,
            "quality": 5,
            "smooth_level": 5,
            "smart_encode": False,
        },
    },
    "custom": {
        "id": "custom",
        "name": "Пользовательский",
        "description": "Ручные параметры потоков",
        "codec": "H264",
        "main": {
            "id": 1,
            "name": "stream1",
            "enabled": True,
            "resolution": "1920x1080",
            "fps": 20,
            "codec": "H264",
            "bitrate": 4096,
            "bitrate_type": "CBR",
            "gop": 40,
            "quality": 5,
            "smooth_level": 5,
            "smart_encode": False,
        },
        "sub": {
            "id": 2,
            "name": "stream2",
            "enabled": True,
            "resolution": "720x576",
            "fps": 10,
            "codec": "H264",
            "bitrate": 512,
            "bitrate_type": "CBR",
            "gop": 20,
            "quality": 4,
            "smooth_level": 5,
            "smart_encode": False,
        },
    },
}


def get_profile(key: str, custom_profiles: dict = None) -> dict:
    profiles = copy.deepcopy(DEFAULT_PROFILES)
    if custom_profiles:
        profiles.update(custom_profiles)
    return profiles.get(key, profiles["medium"])


def list_profile_items(custom_profiles: dict = None) -> list[tuple[str, str]]:
    profiles = copy.deepcopy(DEFAULT_PROFILES)
    if custom_profiles:
        profiles.update(custom_profiles)
    return [(k, v["name"]) for k, v in profiles.items()]


def profile_to_streams_config(profile: dict, force_codec: str = None) -> list[dict]:
    """Convert a video profile into list of streams for camera_configurator drivers."""
    streams = []
    main = copy.deepcopy(profile.get("main", {}))
    sub = copy.deepcopy(profile.get("sub", {}))
    codec = force_codec or profile.get("codec", "H264")
    if main:
        main["codec"] = codec
        streams.append(main)
    if sub:
        sub["codec"] = codec
        streams.append(sub)
    return streams


def estimate_traffic_summary(camera_count: int, profile: dict) -> dict:
    """Calculate estimated theoretical traffic for a given number of cameras."""
    main_kbps = profile.get("main", {}).get("bitrate", 3000)
    sub_kbps = profile.get("sub", {}).get("bitrate", 512)
    total_main_mbps = round((main_kbps * camera_count) / 1000.0, 1)
    total_sub_mbps = round((sub_kbps * camera_count) / 1000.0, 1)
    total_both_mbps = round(((main_kbps + sub_kbps) * camera_count) / 1000.0, 1)
    return {
        "camera_count": camera_count,
        "main_kbps": main_kbps,
        "sub_kbps": sub_kbps,
        "total_main_mbps": total_main_mbps,
        "total_sub_mbps": total_sub_mbps,
        "total_both_mbps": total_both_mbps,
        "note": "Расчётный объём трафика (не физическое измерение сети).",
    }


def save_profiles_file(path: pathlib.Path, profiles: dict) -> None:
    data = copy.deepcopy(profiles)
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    else:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def load_profiles_file(path: pathlib.Path) -> dict:
    suffix = path.suffix.lower()
    with path.open("r", encoding="utf-8") as f:
        if suffix in (".yaml", ".yml"):
            return yaml.safe_load(f) or {}
        return json.load(f) or {}
