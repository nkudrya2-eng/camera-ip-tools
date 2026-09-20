"""Embedded VLC video preview widget for CameraIpTools using libvlc via ctypes."""
import ctypes
import os
import pathlib
import sys
import tkinter as tk
from tkinter import ttk
import urllib.parse

from camera_video import find_vlc, launch_video, stream_uri


class LibVlcWrapper:
    """Minimal ctypes wrapper around libvlc.dll for video playback in Tkinter window."""

    _instance = None
    _lib = None
    _available = False
    _error_message = ""

    @classmethod
    def initialize(cls):
        if cls._lib is not None:
            return cls._available

        candidates = [
            r"C:\Program Files\VideoLAN\VLC",
            r"C:\Program Files (x86)\VideoLAN\VLC",
        ]
        found_dir = None
        for d in candidates:
            if (pathlib.Path(d) / "libvlc.dll").is_file():
                found_dir = d
                break

        if not found_dir:
            cls._available = False
            cls._error_message = "Библиотека libvlc.dll не найдена (установите 64-битный VLC)."
            return False

        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(found_dir)
            cls._lib = ctypes.CDLL(str(pathlib.Path(found_dir) / "libvlc.dll"))

            # Signature setup
            cls._lib.libvlc_new.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]
            cls._lib.libvlc_new.restype = ctypes.c_void_p

            cls._lib.libvlc_release.argtypes = [ctypes.c_void_p]
            cls._lib.libvlc_release.restype = None

            cls._lib.libvlc_media_new_location.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            cls._lib.libvlc_media_new_location.restype = ctypes.c_void_p

            cls._lib.libvlc_media_release.argtypes = [ctypes.c_void_p]
            cls._lib.libvlc_media_release.restype = None

            cls._lib.libvlc_media_player_new.argtypes = [ctypes.c_void_p]
            cls._lib.libvlc_media_player_new.restype = ctypes.c_void_p

            cls._lib.libvlc_media_player_release.argtypes = [ctypes.c_void_p]
            cls._lib.libvlc_media_player_release.restype = None

            cls._lib.libvlc_media_player_set_media.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            cls._lib.libvlc_media_player_set_media.restype = None

            cls._lib.libvlc_media_player_set_hwnd.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            cls._lib.libvlc_media_player_set_hwnd.restype = None

            cls._lib.libvlc_media_player_play.argtypes = [ctypes.c_void_p]
            cls._lib.libvlc_media_player_play.restype = ctypes.c_int

            cls._lib.libvlc_media_player_stop.argtypes = [ctypes.c_void_p]
            cls._lib.libvlc_media_player_stop.restype = None

            cls._lib.libvlc_media_player_is_playing.argtypes = [ctypes.c_void_p]
            cls._lib.libvlc_media_player_is_playing.restype = ctypes.c_int

            # Create instance with low latency network caching args
            args = [
                b"--no-xlib",
                b"--no-video-title-show",
                b"--network-caching=300",
                b"--rtsp-tcp",
            ]
            arr = (ctypes.c_char_p * len(args))(*args)
            cls._instance = cls._lib.libvlc_new(len(args), arr)
            cls._available = cls._instance is not None
            return cls._available
        except Exception as exc:
            cls._available = False
    @classmethod
    def is_available(cls) -> bool:
        if cls._lib is None:
            cls.initialize()
        return cls._available

    @classmethod
    def get_error(cls) -> str:
        return cls._error_message


class EmbeddedVlcPlayer(ttk.Frame):
    """Tkinter frame embedding VLC playback in the right bottom pane."""

    def __init__(self, parent, on_external_request=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.on_external_request = on_external_request
        self.current_camera = None
        self.current_uri = ""
        self.current_stream_type = "sub"  # "main" or "sub"
        self.vlc_player = None
        self.vlc_media = None
        self.is_playing = False

        self._setup_ui()
        LibVlcWrapper.initialize()

    @property
    def vlc_available(self) -> bool:
        return LibVlcWrapper.is_available()

    @property
    def is_available(self) -> bool:
        return LibVlcWrapper.is_available()

    def _setup_ui(self):
        # Header toolbar
        self.header = ttk.Frame(self)
        self.header.pack(fill="x", padx=4, pady=2)

        self.title_label = ttk.Label(self.header, text="📹 Видео выбранной камеры", font=("Segoe UI", 11, "bold"))
        self.title_label.pack(side="left", padx=2)

        self.stream_type_var = tk.StringVar(value="sub")
        self.btn_stream = ttk.Combobox(
            self.header,
            textvariable=self.stream_type_var,
            values=["sub (доп)", "main (основной)"],
            width=14,
            state="readonly",
        )
        self.btn_stream.pack(side="left", padx=6)
        self.btn_stream.bind("<<ComboboxSelected>>", self._on_stream_type_changed)

        self.btn_play = ttk.Button(self.header, text="▶ Пуск", width=7, command=self._on_play_click)
        self.btn_play.pack(side="left", padx=2)

        self.btn_stop = ttk.Button(self.header, text="■ Стоп", width=7, command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=2)

        self.btn_external = ttk.Button(self.header, text="Внешний VLC", width=12, command=self._on_external_click)
        self.btn_external.pack(side="right", padx=2)

        # Video container
        self.video_container = tk.Frame(self, bg="#1e2227")
        self.video_container.pack(fill="both", expand=True, padx=4, pady=2)

        # Placeholder canvas shown when no video is playing
        self.placeholder = tk.Canvas(self.video_container, bg="#1e2227", highlightthickness=0)
        self.placeholder.pack(fill="both", expand=True)
        self.placeholder_text = self.placeholder.create_text(
            150, 100,
            text="Видео остановлено\n\nВыберите камеру в таблице и нажмите «▶ Пуск»",
            fill="#8a939e",
            font=("Segoe UI", 10),
            justify="center",
        )
        self.placeholder.bind("<Configure>", self._center_placeholder)

        # Real drawing surface for libvlc
        self.video_surface = tk.Frame(self.video_container, bg="#000000")

    def _center_placeholder(self, event=None):
        w = self.placeholder.winfo_width()
        h = self.placeholder.winfo_height()
        if w > 10 and h > 10:
            self.placeholder.coords(self.placeholder_text, w // 2, h // 2)

    def set_camera(self, camera: dict):
        """Update selected camera reference without automatically playing."""
        self.current_camera = camera
        if camera:
            ip = camera.get("ip", "?")
            model = camera.get("model", "")
            label = f"📹 {ip}" + (f" ({model})" if model else "")
            self.title_label.configure(text=label)
            if not self.is_playing:
                self.placeholder.itemconfigure(
                    self.placeholder_text,
                    text=f"Камера: {ip}\nНажмите «▶ Пуск» для открытия видеопотока",
                )
        else:
            self.title_label.configure(text="📹 Видео выбранной камеры")
            if not self.is_playing:
                self.placeholder.itemconfigure(
                    self.placeholder_text,
                    text="Камера не выбрана",
                )

    def _on_stream_type_changed(self, event=None):
        val = self.stream_type_var.get()
        new_type = "main" if "main" in val else "sub"
        if new_type != self.current_stream_type:
            self.current_stream_type = new_type
            if self.is_playing and self.current_camera:
                self.play(self.current_camera, stream_type=self.current_stream_type)

    def _on_play_click(self):
        if self.current_camera:
            self.play(self.current_camera, stream_type=self.current_stream_type)
        else:
            self.placeholder.itemconfigure(self.placeholder_text, text="Сначала выберите камеру в таблице")

    def _on_external_click(self):
        if self.current_camera:
            if self.on_external_request:
                self.on_external_request(self.current_camera)
            else:
                vlc_path = find_vlc()
                if not vlc_path:
                    self.placeholder.itemconfigure(
                        self.placeholder_text,
                        text="Внешний плеер VLC не найден на компьютере."
                    )
                    return
                ip = self.current_camera.get("ip")
                u = self.current_camera.get("username", "Admin")
                p = self.current_camera.get("password", "1234")
                try:
                    uri = stream_uri(ip, u, p)
                    launch_video(vlc_path, uri, u, p)
                except Exception as e:
                    self.placeholder.itemconfigure(self.placeholder_text, text=f"Ошибка открытия VLC: {e}")

    def play(self, camera: dict, stream_type: str = "sub"):
        self.stop()
        self.current_camera = camera
        self.current_stream_type = stream_type

        ip = camera.get("ip")
        if not ip:
            return

        username = camera.get("username", "Admin")
        password = camera.get("password", "1234")

        # Check LibVLC availability
        if not LibVlcWrapper.initialize():
            self.placeholder.pack(fill="both", expand=True)
            self.placeholder.itemconfigure(
                self.placeholder_text,
                text=f"Встроенный плеер недоступен:\n{LibVlcWrapper._error_message}\n\nИспользуйте кнопку «Внешний VLC».",
            )
            return

        # Obtain RTSP URI
        self.placeholder.itemconfigure(self.placeholder_text, text=f"Подключение к RTSP ({ip})...")
        try:
            # Fallback to direct RTSP URI generation if ONVIF is slow
            uri = self._resolve_stream_uri(ip, username, password, stream_type)
        except Exception as exc:
            self.placeholder.itemconfigure(
                self.placeholder_text,
                text=f"Не удалось получить поток RTSP с {ip}:\n{exc}\n\nПопробуйте «Внешний VLC».",
            )
            return

        try:
            # Embed video surface
            self.placeholder.pack_forget()
            self.video_surface.pack(fill="both", expand=True)
            self.video_surface.update()

            lib = LibVlcWrapper._lib
            instance = LibVlcWrapper._instance

            # Embed credentials into URI for libvlc
            parsed = urllib.parse.urlsplit(uri)
            cred = urllib.parse.quote(username, safe="") + ":" + urllib.parse.quote(password, safe="") + "@"
            auth_uri = urllib.parse.urlunsplit((parsed.scheme, cred + parsed.netloc, parsed.path, parsed.query, ""))

            self.vlc_player = lib.libvlc_media_player_new(instance)
            self.vlc_media = lib.libvlc_media_new_location(instance, auth_uri.encode("utf-8"))
            lib.libvlc_media_player_set_media(self.vlc_player, self.vlc_media)

            hwnd = ctypes.c_void_p(self.video_surface.winfo_id())
            lib.libvlc_media_player_set_hwnd(self.vlc_player, hwnd)
            lib.libvlc_media_player_play(self.vlc_player)

            self.is_playing = True
            self.btn_play.configure(state="disabled")
            self.btn_stop.configure(state="normal")
        except Exception as exc:
            self.stop()
            self.placeholder.itemconfigure(self.placeholder_text, text=f"Ошибка воспроизведения: {exc}")

    def _resolve_stream_uri(self, ip: str, username: str, password: str, stream_type: str) -> str:
        """Attempt ONVIF stream resolution with standard RTSP fallback."""
        stream_idx = 0 if stream_type == "main" else 1
        try:
            return stream_uri(ip, username, password, stream_index=stream_idx)
        except Exception:
            channel_idx = 1 if stream_type == "main" else 2
            return f"rtsp://{ip}:554/ch01/{channel_idx}"

    def stop(self):
        """Stop video playback and free all resources."""
        if self.vlc_player and LibVlcWrapper._lib:
            try:
                LibVlcWrapper._lib.libvlc_media_player_stop(self.vlc_player)
                LibVlcWrapper._lib.libvlc_media_player_release(self.vlc_player)
            except Exception:
                pass
            self.vlc_player = None

        if self.vlc_media and LibVlcWrapper._lib:
            try:
                LibVlcWrapper._lib.libvlc_media_release(self.vlc_media)
            except Exception:
                pass
            self.vlc_media = None

        self.is_playing = False
        self.video_surface.pack_forget()
        self.placeholder.pack(fill="both", expand=True)
        self.btn_play.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        if self.current_camera:
            self.set_camera(self.current_camera)
