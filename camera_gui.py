import copy
import csv
import concurrent.futures
import datetime as dt
import ipaddress
import os
import pathlib
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont

import yaml

APP_DIR = pathlib.Path(__file__).resolve().parent
LOCAL_CONFIGURATOR_DIR = APP_DIR.parent / "AutoStartSettingWork_v0.02"
CONFIGURATOR_DIR = LOCAL_CONFIGURATOR_DIR if LOCAL_CONFIGURATOR_DIR.exists() else APP_DIR
sys.path.insert(0, str(CONFIGURATOR_DIR))

import camera_configurator as configurator
from camera_inventory_reader import probe_onvif_camera, read_camera_settings
from camera_store import CameraStore, camera_identity
from camera_table_tools import FIELDS, matches, clear_readings, export_rows
from camera_video import find_vlc, stream_uri, launch_video, camera_url


DEFAULT_CONFIG = CONFIGURATOR_DIR / "camera_config.yaml"
DATABASE_PATH = APP_DIR / "camera_tools.db"
SCAN_PASSES = 3
DEFAULT_CAMERA_IP = "192.168.0.250"
DEFAULT_ASSIGNMENT_LOG = APP_DIR / "default_ip_assignment_results.csv"
OPERATIONS = [
    ("network", "Сеть", "apply_network"),
    ("timezone", "Часовой пояс", "apply_timezone"),
    ("ntp", "NTP", "apply_ntp"),
    ("motion", "Детектор движения", "apply_motion"),
    ("streams", "Потоки видео", "apply_streams"),
]
TIMEZONE_VALUES = [f"(GMT{offset:+03d}:00)" for offset in range(-12, 15)]
TIMEZONE_VALUES[TIMEZONE_VALUES.index("(GMT+09:00)")] = "(GMT+09:00) Якутия"


class QueueWriter:
    def __init__(self, output_queue: queue.Queue):
        self.output_queue = output_queue

    def write(self, value: str) -> int:
        if value:
            self.output_queue.put(("log", value))
        return len(value)

    def flush(self) -> None:
        pass


class CameraGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Camera IP Tools — 2026.09.3")
        self.geometry("1380x850")
        self.minsize(1100, 760)

        self.config_data = {}
        self.cameras = []
        self.inventory = []
        self.events = queue.Queue()
        self.running = False
        self.cancel_requested = threading.Event()
        self.current_project_name = ""
        self.active_credential_rules = []
        self.sort_column = ""
        self.sort_reverse = False
        self.table_headings = {}
        self.column_filters = {}
        self.scan_time = ""
        self.video_pending = False
        self.store = CameraStore(DATABASE_PATH)

        configurator.PROFILES_DIR = CONFIGURATOR_DIR / "profiles"
        self._configure_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_events)
        if DEFAULT_CONFIG.exists():
            self.config_path.set(str(DEFAULT_CONFIG))
            self.load_config()
        self._load_projects()

    def _configure_style(self) -> None:
        for font_name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(font_name).configure(size=10)
        tkfont.nametofont("TkHeadingFont").configure(size=10, weight="bold")
        tkfont.nametofont("TkFixedFont").configure(size=10)
        style = ttk.Style(self)
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=26)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", padding=(8, 5))
        style.configure("TEntry", padding=4)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=3)
        self.rowconfigure(5, weight=2)

        source = ttk.Frame(self, padding=(12, 12, 12, 6))
        source.grid(row=0, column=0, sticky="ew")
        source.columnconfigure(1, weight=1)
        ttk.Label(source, text="Объект").grid(row=0, column=0, padx=(0, 8))
        self.project_name = tk.StringVar()
        self.project_box = ttk.Combobox(source, textvariable=self.project_name, state="readonly")
        self.project_box.grid(row=0, column=1, sticky="ew")
        self.project_box.bind("<<ComboboxSelected>>", self._project_changed)
        ttk.Button(source, text="Новый", command=self._new_project).grid(row=0, column=2, padx=6)
        ttk.Button(source, text="Удалить", command=self._delete_project).grid(row=0, column=3)

        ttk.Label(source, text="Конфигурация").grid(row=1, column=0, padx=(0, 8), pady=(8, 0))
        self.config_path = tk.StringVar()
        ttk.Entry(source, textvariable=self.config_path).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(source, text="Обзор", command=self.choose_config).grid(row=1, column=2, padx=6, pady=(8, 0))
        ttk.Button(source, text="Загрузить", command=self.load_config).grid(row=1, column=3, pady=(8, 0))

        scan = ttk.Frame(self, padding=(12, 6))
        scan.grid(row=1, column=0, sticky="ew")
        for column in (2, 5, 8):
            scan.columnconfigure(column, weight=1)
        self.vendor_discovery = tk.BooleanVar(value=False)
        ttk.Checkbutton(scan, text="Поиск производителя", variable=self.vendor_discovery).grid(
            row=0, column=0, padx=(0, 12), sticky="w"
        )
        ttk.Label(scan, text="Сканировать от").grid(row=0, column=1, padx=(0, 4))
        self.scan_start = tk.StringVar(value="192.168.0.250")
        ttk.Entry(scan, textvariable=self.scan_start, width=16).grid(row=0, column=2, sticky="ew")
        ttk.Label(scan, text="до").grid(row=0, column=3, padx=4)
        self.scan_end = tk.StringVar()
        ttk.Entry(scan, textvariable=self.scan_end, width=16).grid(row=0, column=4, sticky="ew")
        ttk.Label(scan, text="IP интерфейса").grid(row=0, column=5, padx=(12, 4), sticky="e")
        self.interface_ip = tk.StringVar()
        ttk.Entry(scan, textvariable=self.interface_ip, width=16).grid(row=0, column=6, sticky="ew")
        self.scan_button = ttk.Button(scan, text="Сканировать", command=self.start_scan)
        self.scan_button.grid(row=0, column=7, padx=(12, 0))

        ttk.Label(scan, text="Назначать от").grid(row=1, column=1, padx=(0, 4), pady=(8, 0))
        self.target_start = tk.StringVar()
        ttk.Entry(scan, textvariable=self.target_start, width=16).grid(row=1, column=2, sticky="ew", pady=(8, 0))
        ttk.Label(scan, text="до").grid(row=1, column=3, padx=4, pady=(8, 0))
        self.target_end = tk.StringVar()
        ttk.Entry(scan, textvariable=self.target_end, width=16).grid(row=1, column=4, sticky="ew", pady=(8, 0))
        ttk.Label(scan, text="Прогонов").grid(row=1, column=5, padx=(12, 4), pady=(8, 0), sticky="e")
        self.pass_count = tk.StringVar(value="1")
        ttk.Combobox(
            scan,
            textvariable=self.pass_count,
            values=("1", "2", "3", "без ограничения"),
            state="readonly",
            width=18,
        ).grid(row=1, column=6, sticky="ew", pady=(8, 0))
        ttk.Button(scan, text="Сформировать план", command=self.build_address_plan).grid(
            row=1, column=7, padx=(12, 0), pady=(8, 0)
        )

        ttk.Label(scan, text="Маска").grid(row=2, column=1, padx=(0, 4), pady=(8, 0))
        self.network_mask = tk.StringVar()
        ttk.Entry(scan, textvariable=self.network_mask, width=16).grid(row=2, column=2, sticky="ew", pady=(8, 0))
        ttk.Label(scan, text="Шлюз").grid(row=2, column=3, padx=4, pady=(8, 0))
        self.gateway = tk.StringVar()
        ttk.Entry(scan, textvariable=self.gateway, width=16).grid(row=2, column=4, sticky="ew", pady=(8, 0))
        ttk.Label(scan, text="DNS").grid(row=2, column=5, padx=(12, 4), pady=(8, 0), sticky="e")
        self.dns_main = tk.StringVar()
        ttk.Entry(scan, textvariable=self.dns_main, width=16).grid(row=2, column=6, sticky="ew", pady=(8, 0))

        settings = ttk.Frame(self, padding=(12, 6))
        settings.grid(row=2, column=0, sticky="ew")
        settings.columnconfigure(7, weight=1)
        self.operation_vars = {}
        for column, (key, label, _method) in enumerate(OPERATIONS):
            var = tk.BooleanVar(value=False)
            self.operation_vars[key] = var
            ttk.Checkbutton(settings, text=label, variable=var).grid(row=0, column=column, padx=(0, 12))
        ttk.Label(settings, text="NTP").grid(row=0, column=5, padx=(12, 4))
        self.ntp_server = tk.StringVar()
        ttk.Entry(settings, textvariable=self.ntp_server, width=18).grid(row=0, column=6)
        ttk.Label(settings, text="Пояс CROSS").grid(row=0, column=7, padx=(12, 4), sticky="e")
        self.cross_timezone = tk.StringVar()
        self.timezone_box = ttk.Combobox(
            settings,
            textvariable=self.cross_timezone,
            values=TIMEZONE_VALUES,
            state="readonly",
            width=25,
        )
        self.timezone_box.grid(row=0, column=8)
        ttk.Label(settings, text="Логин").grid(row=1, column=0, padx=(0, 4), pady=(8, 0), sticky="w")
        self.default_username = tk.StringVar(value="Admin")
        ttk.Entry(settings, textvariable=self.default_username, width=16).grid(
            row=1, column=1, padx=(0, 12), pady=(8, 0), sticky="w"
        )
        ttk.Label(settings, text="Пароль").grid(row=1, column=2, padx=(0, 4), pady=(8, 0), sticky="w")
        self.default_password = tk.StringVar(value="1234")
        ttk.Entry(settings, textvariable=self.default_password, width=18, show="•").grid(
            row=1, column=3, padx=(0, 12), pady=(8, 0), sticky="w"
        )
        ttk.Label(settings, text="Исключения доступа").grid(
            row=1, column=4, padx=(12, 4), pady=(8, 0), sticky="e"
        )
        self.credential_exceptions = tk.StringVar()
        ttk.Entry(settings, textvariable=self.credential_exceptions).grid(
            row=1, column=5, columnspan=4, pady=(8, 0), sticky="ew"
        )

        table_frame = ttk.Frame(self, padding=(12, 6))
        table_frame.grid(row=3, column=0, sticky="nsew")
        table_frame.rowconfigure(1, weight=1)
        table_frame.columnconfigure(0, weight=1)
        columns = (
            "assign", "ip", "new_ip", "model", "mac", "serial", "mask", "gateway_read",
            "ntp_read", "timezone_read", "codec", "profile", "seen", "status",
        )
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        for key, label, width in [
            ("assign", "Назначить", 75),
            ("ip", "Текущий IP", 120),
            ("new_ip", "Новый IP", 120),
            ("model", "Модель", 260),
            ("mac", "MAC", 140),
            ("serial", "S/N / DeviceID", 150),
            ("mask", "Маска", 120),
            ("gateway_read", "Шлюз камеры", 120),
            ("ntp_read", "NTP камеры", 130),
            ("timezone_read", "Часовой пояс", 150),
            ("codec", "Кодек", 80),
            ("profile", "Профиль", 100),
            ("seen", "Ответы", 70),
            ("status", "Статус", 180),
        ]:
            self.table_headings[key] = label
            self.table.heading(key, text=label, command=lambda column=key: self._sort_table(column))
            self.table.column(key, width=width, anchor="w")
        filters = ttk.Frame(table_frame)
        filters.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text="Найти в таблице").grid(row=0, column=0, padx=(0, 8))
        self.filter_text = tk.StringVar()
        ttk.Entry(filters, textvariable=self.filter_text).grid(row=0, column=1, sticky="ew")
        self.filter_field = tk.StringVar(value="Все поля")
        self.filter_fields = {"Все поля": "", **{label: key for key, label in self.table_headings.items() if key != "assign"}}
        ttk.Combobox(filters, textvariable=self.filter_field, values=list(self.filter_fields),
                     state="readonly", width=20).grid(row=0, column=2, padx=6)
        ttk.Button(filters, text="Сбросить фильтры", command=self._reset_filters).grid(row=0, column=3)
        ttk.Button(filters, text="История", command=self._show_history).grid(row=0, column=4, padx=6)
        self.export_scope = tk.StringVar(value="Показанные строки")
        ttk.Combobox(filters, textvariable=self.export_scope,
                     values=("Показанные строки", "Весь текущий список", "Отмеченные строки"),
                     state="readonly", width=23).grid(row=0, column=5)
        self.table_summary = tk.StringVar()
        ttk.Label(filters, textvariable=self.table_summary).grid(row=1, column=0, columnspan=6, sticky="w", pady=(5, 0))
        self.filter_text.trace_add("write", self._filter_changed)
        self.filter_field.trace_add("write", self._filter_changed)
        self.table.grid(row=1, column=0, sticky="nsew")
        self.table.bind("<Button-3>", self._context_menu)
        self.context_menu = tk.Menu(self, tearoff=False)
        self.context_menu.add_command(label="Открыть видео (VLC)", command=self._open_video)
        self.context_menu.add_command(label="Открыть в браузере", command=self._open_selected_web)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Убрать из списка", command=self._remove_selected)
        self.table.bind("<Button-1>", self._table_click, add=True)
        self.table.bind("<Double-1>", self._open_camera_web, add=True)
        self.table.bind("<Control-c>", self.copy_table_rows)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=2, column=0, sticky="ew")
        self.table.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)

        actions = ttk.Frame(self, padding=(12, 6))
        actions.grid(row=4, column=0, sticky="ew")
        self.select_all_button = ttk.Button(actions, text="Отметить все", command=self.select_all)
        self.select_all_button.pack(side="left")
        self.test_button = ttk.Button(actions, text="Проверить подключение", command=self.test_selected)
        self.test_button.pack(side="left", padx=8)
        self.details_button = ttk.Button(actions, text="Обновить данные", command=self.refresh_details)
        self.details_button.pack(side="left", padx=(0, 8))
        self.apply_button = ttk.Button(actions, text="Применить выбранное", command=self.apply_selected)
        self.apply_button.pack(side="left")
        self.address_button = ttk.Button(actions, text="Назначить IP", command=self.apply_address_plan)
        self.address_button.pack(side="left", padx=(8, 0))
        self.default_ip_button = ttk.Button(
            actions,
            text="Назначить 192.168.0.250",
            command=self.apply_default_ip_plan,
        )
        self.default_ip_button.pack(side="left", padx=(8, 0))
        self.export_button = ttk.Button(actions, text="Excel / CSV", command=self.export_inventory)
        self.export_button.pack(side="left", padx=(8, 0))
        self.copy_button = ttk.Button(actions, text="Копировать", command=self.copy_table_rows)
        self.copy_button.pack(side="left", padx=8)
        # Two rows keep every action reachable on ordinary laptop/server displays.
        for button in actions.winfo_children():
            button.pack_forget()
        for index, button in enumerate((self.select_all_button, self.test_button, self.details_button,
                                        self.apply_button, self.address_button, self.default_ip_button,
                                        self.export_button, self.copy_button)):
            button.grid(row=0, column=index, padx=(0, 6), pady=3, sticky="w")
        self.status = tk.StringVar(value="Готово")
        progress_frame = ttk.Frame(self, padding=(12, 4))
        progress_frame.grid(row=6, column=0, sticky="ew")
        progress_frame.columnconfigure(0, weight=1)
        ttk.Label(progress_frame, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(progress_frame, length=160, mode="determinate")
        self.progress_bar.grid(row=0, column=1, padx=8)
        self.stop_button = ttk.Button(progress_frame, text="Остановить", command=self._request_stop, state="disabled")
        self.stop_button.grid(row=0, column=2)

        log_frame = ttk.Frame(self, padding=(12, 6, 12, 12))
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)

    def choose_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите конфигурацию",
            initialdir=str(APP_DIR),
            filetypes=[("YAML", "*.yaml *.yml"), ("Все файлы", "*.*")],
        )
        if path:
            self.config_path.set(path)
            self.load_config()

    def load_config(self) -> None:
        if self.running:
            return
        try:
            path = pathlib.Path(self.config_path.get()).expanduser().resolve()
            with path.open(encoding="utf-8") as stream:
                data = yaml.safe_load(stream) or {}
            cameras = configurator.expand_cameras(data.get("cameras") or [data.get("camera")])
            cameras = [camera for camera in cameras if camera]
        except Exception as exc:
            messagebox.showerror("Ошибка конфигурации", str(exc))
            return

        self.config_data = data
        self.cameras = cameras
        # YAML supplies configuration and credentials, never a live device list.
        self.ntp_server.set(str(data.get("ntp", {}).get("server", "")))
        self.cross_timezone.set(str(data.get("timezone", {}).get("timezone", "")))
        network = data.get("network", {})
        self.network_mask.set(str(network.get("subnet_mask", "255.255.255.0")))
        self.gateway.set(str(network.get("gateway", "")))
        self.dns_main.set(str(network.get("dns_main", "")))
        if cameras:
            self.default_username.set(str(cameras[0].get("username", "Admin")))
            self.default_password.set(str(cameras[0].get("password", "")))
        if self.cross_timezone.get() not in TIMEZONE_VALUES:
            self.timezone_box.configure(values=[self.cross_timezone.get(), *TIMEZONE_VALUES])
        self._populate_table()
        self.status.set("Конфигурация загружена. Для поиска камер нажмите «Сканировать».")

    def _load_projects(self) -> None:
        names = self.store.list_projects()
        if not names:
            self.store.ensure_project("Основной объект")
            names = ["Основной объект"]
        self.project_box.configure(values=names)
        self.project_name.set(names[0])
        self._load_project(names[0])

    def _project_settings(self) -> dict:
        columns = {key: self.table.column(key, "width") for key in self.table["columns"]}
        return {
            "scan_start": self.scan_start.get().strip(),
            "scan_end": self.scan_end.get().strip(),
            "interface_ip": self.interface_ip.get().strip(),
            "vendor_discovery": bool(self.vendor_discovery.get()),
            "target_start": self.target_start.get().strip(),
            "target_end": self.target_end.get().strip(),
            "pass_count": self.pass_count.get(),
            "network_mask": self.network_mask.get().strip(),
            "gateway": self.gateway.get().strip(),
            "dns_main": self.dns_main.get().strip(),
            "ntp_server": self.ntp_server.get().strip(),
            "timezone": self.cross_timezone.get().strip(),
            "operations": {key: bool(value.get()) for key, value in self.operation_vars.items()},
            "geometry": self.geometry(),
            "columns": columns,
        }

    def _apply_project_settings(self, settings: dict) -> None:
        variables = {
            "scan_start": self.scan_start,
            "scan_end": self.scan_end,
            "interface_ip": self.interface_ip,
            "target_start": self.target_start,
            "target_end": self.target_end,
            "pass_count": self.pass_count,
            "network_mask": self.network_mask,
            "gateway": self.gateway,
            "dns_main": self.dns_main,
            "ntp_server": self.ntp_server,
            "timezone": self.cross_timezone,
        }
        for key, variable in variables.items():
            if key in settings:
                variable.set(settings[key])
        if "vendor_discovery" in settings:
            self.vendor_discovery.set(bool(settings["vendor_discovery"]))
        # Applying settings is an explicit decision in every new session.
        for variable in self.operation_vars.values():
            variable.set(False)
        for key, width in settings.get("columns", {}).items():
            if key in self.table["columns"]:
                self.table.column(key, width=int(width))
        geometry = settings.get("geometry", "")
        if geometry:
            try:
                self.geometry(geometry)
            except tk.TclError:
                pass

    def _load_project(self, name: str) -> None:
        settings, cameras = self.store.load_project(name)
        self.current_project_name = name
        # Reset missing settings too, so a new object does not inherit another one's network.
        defaults = {"scan_start": "192.168.0.250", "scan_end": "", "interface_ip": "",
                    "target_start": "", "target_end": "", "pass_count": "1",
                    "network_mask": "", "gateway": "", "dns_main": "", "ntp_server": "",
                    "timezone": "", "vendor_discovery": False}
        self._apply_project_settings({**defaults, **settings})
        self.inventory = []
        self.scan_time = ""
        self._reset_filters()
        self.status.set(f"Объект «{name}»: поиск ещё не выполнен. В истории: {len(cameras)}.")

    def _save_current_project(self) -> None:
        if self.current_project_name:
            self.store.save_project(
                self.current_project_name,
                self._project_settings(),
                self.inventory,
            )

    def _project_changed(self, _event=None) -> None:
        if self.running:
            return
        name = self.project_name.get().strip()
        if not name or name == self.current_project_name:
            return
        self._save_current_project()
        self._load_project(name)

    def _new_project(self) -> None:
        if self.running:
            return
        name = simpledialog.askstring("Новый объект", "Название объекта:", parent=self)
        if not name:
            return
        name = name.strip()
        if name in self.store.list_projects():
            messagebox.showwarning("Объект существует", "Объект с таким названием уже создан.")
            return
        self._save_current_project()
        self.store.ensure_project(name)
        names = self.store.list_projects()
        self.project_box.configure(values=names)
        self.project_name.set(name)
        self.inventory = []
        self._load_project(name)

    def _delete_project(self) -> None:
        if self.running:
            return
        name = self.project_name.get().strip()
        if not name or not messagebox.askyesno(
            "Удалить объект", f"Удалить объект «{name}» и его сохраненный список камер?"
        ):
            return
        self.store.delete_project(name)
        self.current_project_name = ""
        self.inventory = []
        self._load_projects()

    def _on_close(self) -> None:
        if self.running:
            messagebox.showinfo("Операция выполняется", "Дождитесь завершения текущей операции перед закрытием.")
            return
        self._save_current_project()
        self.store.close()
        self.destroy()

    def _inventory_from_camera(self, camera: dict) -> dict:
        return {
            "ip": camera.get("ip", ""),
            "assign": False,
            "new_ip": "",
            "model": camera.get("model", camera.get("name", "")),
            "mac": camera.get("mac", ""),
            "device_id": camera.get("device_id", ""),
            "serial_number": camera.get("serial_number", ""),
            "mask": "",
            "gateway_read": "",
            "ntp_read": "",
            "timezone_read": "",
            "codec": "",
            "profile": camera.get("profile", "cross"),
            "status": "из конфигурации",
            "seen_passes": 0,
            "online": False,
            "camera": camera,
            "vendor_row": None,
        }

    def _populate_table(self) -> None:
        old_rows = getattr(self, "_display_rows", {})
        selected = {id(old_rows[item]) for item in self.table.selection() if item in old_rows}
        self._display_rows = {}
        for item in self.table.get_children():
            self.table.delete(item)
        for index, row in enumerate(self.inventory):
            if not self._matches_filter(row):
                row["assign"] = False
                row["new_ip"] = ""
                continue
            self._display_rows[str(index)] = row
            self.table.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    "☑" if row.get("assign") else "☐",
                    row.get("ip", ""),
                    row.get("new_ip", ""),
                    row.get("model", ""),
                    row.get("mac", ""),
                    row.get("serial_number") or row.get("device_id", ""),
                    row.get("mask", "") or "не прочитана",
                    row.get("gateway_read", "") or "не прочитан",
                    row.get("ntp_read", "") or "не прочитан",
                    row.get("timezone_read", "") or "не прочитан",
                    row.get("codec", "") or "не прочитан",
                    row.get("profile", "cross"),
                    f"{row.get('seen_passes', 0)}/{SCAN_PASSES}" if row.get("online") else "—",
                    row.get("status", ""),
                ),
            )
            if id(row) in selected:
                self.table.selection_add(str(index))

        shown = len(self.table.get_children())
        marked = sum(bool(row.get("assign")) for row in self._visible_rows())
        filters = "; ".join(f"{self.table_headings.get(k, k)}: {v}" for k, v in self.column_filters.items())
        stamp = f"Поиск: {self.scan_time}" if self.scan_time else "Поиск ещё не выполнен"
        self.table_summary.set(f"Показано {shown} из {len(self.inventory)} · Отмечено: {marked} · {stamp}" + (f" · Фильтры: {filters}" if filters else ""))

    def _sort_value(self, row: dict, column: str):
        values = {
            "assign": row.get("assign", False),
            "ip": row.get("ip", ""),
            "new_ip": row.get("new_ip", ""),
            "model": row.get("model", ""),
            "mac": row.get("mac", ""),
            "serial": row.get("serial_number") or row.get("device_id", ""),
            "mask": row.get("mask", ""),
            "gateway_read": row.get("gateway_read", ""),
            "ntp_read": row.get("ntp_read", ""),
            "timezone_read": row.get("timezone_read", ""),
            "codec": row.get("codec", ""),
            "profile": row.get("profile", "cross"),
            "seen": row.get("seen_passes", 0) if row.get("online") else "",
            "status": row.get("status", ""),
        }
        value = values.get(column, "")
        if isinstance(value, bool):
            return (0, (0, int(value)))
        if isinstance(value, (int, float)):
            return (0, (0, value))
        text = str(value).strip()
        if not text:
            return (1, (0, 0))
        try:
            return (0, (0, int(ipaddress.ip_address(text))))
        except ValueError:
            parts = tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", text))
            return (0, (1, parts))

    def _sort_table(self, column: str) -> None:
        if self.running:
            return
        selected_rows = {
            id(self.inventory[int(item)])
            for item in self.table.selection()
            if item.isdigit() and int(item) < len(self.inventory)
        }
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False

        present = []
        empty = []
        for row in self.inventory:
            key = self._sort_value(row, column)
            (empty if key[0] else present).append((key[1], row))
        present.sort(key=lambda item: item[0], reverse=self.sort_reverse)
        self.inventory[:] = [row for _, row in present] + [row for _, row in empty]

        for key, label in self.table_headings.items():
            marker = " ▲" if key == column and not self.sort_reverse else " ▼" if key == column else ""
            self.table.heading(key, text=label + marker)
        self._populate_table()
        for index, row in enumerate(self.inventory):
            if id(row) in selected_rows and self.table.exists(str(index)):
                self.table.selection_add(str(index))

    def select_all(self) -> None:
        if self.running:
            return
        rows = self._visible_rows()
        should_mark = any(not row.get("assign") for row in rows)
        for row in rows:
            row["assign"] = should_mark
        self._populate_table()
        self.select_all_button.configure(text="Снять отметки" if should_mark else "Отметить все")

    def _table_click(self, event) -> None:
        if self.running:
            return
        if self.table.identify_region(event.x, event.y) != "cell":
            return
        if self.table.identify_column(event.x) != "#1":
            return
        item = self.table.identify_row(event.y)
        if not item:
            return
        row = self.inventory[int(item)]
        row["assign"] = not row.get("assign", False)
        self._populate_table()

    def copy_table_rows(self, event=None):
        if not self.inventory:
            if event is None:
                messagebox.showwarning("Список пуст", "Нет данных для копирования.")
            return "break" if event is not None else None

        items = list(self.table.selection())
        if not items:
            items = [item for item in self.table.get_children() if self.inventory[int(item)].get("assign")]
        if not items:
            items = list(self.table.get_children())
        items.sort(key=self.table.index)

        columns = list(self.table["columns"])
        lines = ["\t".join(self.table_headings[column] for column in columns)]
        for item in items:
            values = self.table.item(item, "values")
            lines.append(
                "\t".join(
                    str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
                    for value in values
                )
            )
        self.clipboard_clear()
        self.clipboard_append("\r\n".join(lines))
        self.update_idletasks()
        self.status.set(f"Скопировано строк: {len(items)}")
        return "break" if event is not None else None

    def _open_camera_web(self, event) -> None:
        item = self.table.identify_row(event.y)
        if not item:
            return
        ip = self.inventory[int(item)].get("ip", "")
        try:
            ipaddress.IPv4Address(ip)
        except ipaddress.AddressValueError:
            return
        webbrowser.open_new_tab(f"http://{ip}")
        self.status.set(f"Открыта камера: {ip}")

    def selected_cameras(self) -> list[dict]:
        marked = [row["camera"] for row in self._visible_rows() if row.get("assign")]
        if marked:
            return marked
        return [self.inventory[int(item)]["camera"] for item in self.table.selection()]


    def _matches_filter(self, row):
        return matches(row, self.filter_text.get(), self.filter_fields.get(self.filter_field.get(), ""), self.column_filters)

    def _visible_rows(self):
        return [self.inventory[int(item)] for item in self.table.get_children()]

    def _filter_changed(self, *_args):
        for row in self.inventory:
            if not self._matches_filter(row):
                row["assign"] = False
                row["new_ip"] = ""
        self._populate_table()

    def _reset_filters(self):
        self.column_filters.clear()
        self.filter_text.set("")
        self.filter_field.set("Все поля")
        self._populate_table()

    def _context_menu(self, event):
        region = self.table.identify_region(event.x, event.y)
        if region == "heading":
            column = int(self.table.identify_column(event.x)[1:]) - 1
            key = self.table["columns"][column]
            if key == "assign" or self.running:
                return "break"
            value = simpledialog.askstring("Фильтр по столбцу", self.table_headings[key] + " содержит (пусто — сбросить):",
                                           initialvalue=self.column_filters.get(key, ""), parent=self)
            if value is not None:
                if value.strip():
                    self.column_filters[key] = value.strip()
                else:
                    self.column_filters.pop(key, None)
                self._filter_changed()
            return "break"
        item = self.table.identify_row(event.y)
        if not item:
            return "break"
        if item not in self.table.selection():
            self.table.selection_set(item)
        self.table.focus(item)
        self.context_menu.entryconfigure(0, state="disabled" if self.running or self.video_pending else "normal")
        self.context_menu.entryconfigure(3, state="disabled" if self.running else "normal")
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
        return "break"

    def _focused_row(self):
        item = self.table.focus()
        if item and self.table.exists(item):
            return self.inventory[int(item)]
        return None

    def _open_selected_web(self):
        row = self._focused_row()
        if row:
            try:
                ip = str(ipaddress.IPv4Address(row["ip"]))
            except ValueError:
                return
            webbrowser.open_new_tab(f"http://{ip}")

    def _open_video(self):
        row = self._focused_row()
        if not row or self.running or self.video_pending or not self._prepare_credentials():
            return
        vlc = find_vlc()
        if not vlc:
            vlc = filedialog.askopenfilename(title="Укажите vlc.exe для просмотра видео", filetypes=[("VLC", "vlc.exe")])
        if not vlc:
            return
        ip = row["ip"]
        username, password = self._credential_for_ip(ip, self.active_credential_rules,
                                                   (self.default_username.get().strip(), self.default_password.get()))
        self.video_pending = True
        self.status.set(f"Получение видеопотока {ip}…")
        def worker():
            try:
                uri = stream_uri(ip, username, password)
                self.events.put(("video_ready", (vlc, uri, username, password)))
            except Exception:
                self.events.put(("video_failed", (vlc, ip, username, password)))
        threading.Thread(target=worker, daemon=True).start()

    def _remove_selected(self):
        if self.running:
            return
        selected = {int(item) for item in self.table.selection()}
        if not selected:
            return
        rows = [row for index, row in enumerate(self.inventory) if index in selected]
        self.store.remove_cameras(self.current_project_name, rows)
        self.inventory = [row for index, row in enumerate(self.inventory) if index not in selected]
        self._save_current_project()
        self._populate_table()
        self.status.set(f"Убрано из списка: {len(rows)}. Повторный поиск может обнаружить их снова.")

    def _show_history(self):
        if self.running:
            return
        self._save_current_project()
        _, rows = self.store.load_project(self.current_project_name)
        project_name = self.current_project_name
        window = tk.Toplevel(self)
        window.title(f"История — {self.current_project_name}")
        window.geometry("1000x500")
        ttk.Label(window, text="Сохранённые наблюдения. Доступность сейчас не проверена; настройки из этого окна не применяются.", padding=10).pack(anchor="w")
        frame = ttk.Frame(window)
        frame.pack(fill="both", expand=True)
        table = ttk.Treeview(frame, columns=("ip", "model", "mac", "seen", "status"), show="headings")
        for key, label in (("ip", "IP"), ("model", "Модель"), ("mac", "MAC"), ("seen", "Последний ответ"), ("status", "Последний статус")):
            table.heading(key, text=label)
            table.column(key, width=180)
        bar = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        table.pack(fill="both", expand=True)
        for row in rows:
            table.insert("", "end", values=(row.get("ip", ""), row.get("model", ""), row.get("mac", ""), row.get("last_seen", ""), row.get("status", "")))
        def export_history():
            path = filedialog.asksaveasfilename(parent=window, title="Экспорт истории", initialfile="История_камер.xlsx", defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
            if path:
                try:
                    export_rows(path, rows, project_name, "История — доступность не проверена")
                except Exception as exc:
                    messagebox.showerror("Экспорт", str(exc), parent=window)
        ttk.Button(window, text="Выгрузить историю в Excel", command=export_history).pack(pady=8)

    def _lock_controls(self, locked):
        # Freeze object/configuration/selection during worker mutations.
        if locked:
            self._locked_controls = []
            def visit(parent):
                for widget in parent.winfo_children():
                    if isinstance(widget, (ttk.Button, ttk.Entry, ttk.Combobox, ttk.Checkbutton)):
                        state = str(widget.cget("state"))
                        self._locked_controls.append((widget, state))
                        widget.configure(state="disabled")
                    visit(widget)
            visit(self)
        else:
            for widget, state in getattr(self, "_locked_controls", []):
                if widget.winfo_exists():
                    widget.configure(state=state)
            self._locked_controls = []

    def _parse_credential_rules(self) -> list[tuple[int, int, str, str]]:
        rules = []
        for item in self.credential_exceptions.get().split(";"):
            item = item.strip()
            if not item:
                continue
            target, separator, credential = item.partition("=")
            if not separator or ":" not in credential:
                raise ValueError(
                    "Исключения задаются так: 10.53.240.123-10.53.240.125=admin:пароль"
                )
            username, password = credential.split(":", 1)
            start_text, dash, end_text = target.strip().partition("-")
            start = int(ipaddress.IPv4Address(start_text.strip()))
            end = int(ipaddress.IPv4Address(end_text.strip())) if dash else start
            if end < start or not username.strip():
                raise ValueError(f"Некорректное исключение: {item}")
            rules.append((start, end, username.strip(), password))
        return rules

    @staticmethod
    def _credential_for_ip(
        ip: str,
        rules: list[tuple[int, int, str, str]],
        default: tuple[str, str],
    ) -> tuple[str, str]:
        value = int(ipaddress.IPv4Address(ip))
        for start, end, username, password in rules:
            if start <= value <= end:
                return username, password
        return default

    def _prepare_credentials(self) -> bool:
        try:
            self.active_credential_rules = self._parse_credential_rules()
        except ValueError as exc:
            messagebox.showerror("Ошибка исключений доступа", str(exc))
            return False
        return True

    def start_scan(self) -> None:
        if self.running:
            return
        if not self._prepare_credentials():
            return
        vendor_mode = bool(self.vendor_discovery.get())
        start = end = None
        if not vendor_mode:
            try:
                start = ipaddress.IPv4Address(self.scan_start.get().strip())
                end_text = self.scan_end.get().strip()
                end = ipaddress.IPv4Address(end_text) if end_text else start
                if int(end) < int(start):
                    raise ValueError("Конечный адрес меньше начального.")
                if int(end) - int(start) > 65535:
                    raise ValueError("Диапазон слишком большой.")
            except ValueError as exc:
                messagebox.showerror("Ошибка диапазона", str(exc))
                return
        self._save_current_project()
        self.inventory = []
        self.scan_time = dt.datetime.now().isoformat(timespec="seconds")
        self._reset_filters()
        self._start_worker(
            "Сканирование: проход 1/3",
            self._scan_worker,
            start,
            end,
            vendor_mode,
            self.default_username.get().strip(),
            self.default_password.get(),
            list(self.active_credential_rules),
        )

    def _camera_template_for_ip(self, ip: str) -> dict:
        for camera in self.cameras:
            if camera.get("ip") == ip:
                return copy.deepcopy(camera)
        if self.cameras:
            camera = copy.deepcopy(self.cameras[0])
            camera["ip"] = ip
        else:
            camera = {"ip": ip, "profile": "cross"}
        camera["username"] = self.default_username.get().strip()
        camera["password"] = self.default_password.get()
        return camera

    @staticmethod
    def _host_replies(ip: str) -> bool:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        result = subprocess.run(
            ["ping", "-n", "1", "-w", "500", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startup,
            check=False,
        )
        if result.returncode == 0:
            return True
        try:
            with socket.create_connection((ip, 80), timeout=0.5):
                return True
        except OSError:
            return False

    def _scan_worker(
        self, start, end, vendor_mode: bool, username: str, password: str, credential_rules: list
    ) -> None:
        found_by_identity = {}
        seen_in_passes = {}
        addresses = []
        if not vendor_mode:
            addresses = [str(ipaddress.IPv4Address(value)) for value in range(int(start), int(end) + 1)]

        for pass_number in range(1, SCAN_PASSES + 1):
            mode_name = "Поиск производителя" if vendor_mode else "Сканирование диапазона"
            self.events.put(("status", f"{mode_name}: проход {pass_number}/{SCAN_PASSES}"))
            alive = set()
            if addresses:
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(addresses))) as pool:
                    alive = {ip for ip, replies in zip(addresses, pool.map(self._host_replies, addresses)) if replies}

            vendor_rows = []
            if vendor_mode:
                try:
                    vendor_rows = self._run_vendor_discovery()
                except Exception as exc:
                    self.events.put(("log", f"Проход {pass_number}: ошибка поиска производителя: {exc}\n"))
                    vendor_rows = []
                if alive:
                    vendor_ips = {row.get("current_ip", "") for row in vendor_rows}
                    missing_ips = sorted(alive - vendor_ips, key=lambda value: int(ipaddress.IPv4Address(value)))
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, max(1, len(missing_ips)))) as pool:
                        futures = {
                            pool.submit(
                                probe_onvif_camera,
                                ip,
                                *self._credential_for_ip(ip, credential_rules, (username, password)),
                                4.0,
                            ): ip
                            for ip in missing_ips
                        }
                        direct_rows = []
                        for future in concurrent.futures.as_completed(futures):
                            try:
                                row = future.result()
                            except Exception:
                                row = None
                            if row:
                                direct_rows.append(row)
                        vendor_rows.extend(direct_rows)
                    self.events.put(("log", f"Проход {pass_number}: прямой ONVIF={len(direct_rows)}\n"))
            else:
                # Обычное сканирование диапазона IP (без мультикаст-поиска производителя)
                if alive:
                    alive_ips = sorted(alive, key=lambda value: int(ipaddress.IPv4Address(value)))
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, max(1, len(alive_ips)))) as pool:
                        futures = {
                            pool.submit(
                                probe_onvif_camera,
                                ip,
                                *self._credential_for_ip(ip, credential_rules, (username, password)),
                                3.0,
                            ): ip
                            for ip in alive_ips
                        }
                        direct_rows = []
                        for future in concurrent.futures.as_completed(futures):
                            ip = futures[future]
                            try:
                                row = future.result()
                            except Exception:
                                row = None
                            if row:
                                direct_rows.append(row)
                            else:
                                direct_rows.append({
                                    "selected": "1",
                                    "protocol": "ip",
                                    "current_ip": ip,
                                    "new_ip": "",
                                    "model": "Сетевое устройство",
                                    "mac": "",
                                    "device_id": "",
                                    "serial_number": "",
                                    "device_name": "",
                                    "manufacturer": "",
                                    "firmware": "",
                                    "http_port": "80",
                                    "mask": "",
                                    "gateway": "",
                                    "dns": "",
                                    "status": "отвечает (ping/HTTP)",
                                    "message": "",
                                    "raw_attributes": "{}",
                                })
                        vendor_rows.extend(direct_rows)

            pass_identities = set()
            for vendor_row in vendor_rows:
                ip = vendor_row.get("current_ip", "")
                if addresses and (ip not in alive or ip not in addresses):
                    continue
                identity = camera_identity(vendor_row)
                existing = found_by_identity.get(identity)
                if existing is None or (
                    vendor_row.get("protocol", "").lower() == "sunell"
                    and existing.get("protocol", "").lower() != "sunell"
                ):
                    found_by_identity[identity] = vendor_row
                pass_identities.add(identity)
            for identity in pass_identities:
                seen_in_passes[identity] = seen_in_passes.get(identity, 0) + 1
            ping_text = f", ping/HTTP={len(alive)}" if addresses else ""
            self.events.put(("log", f"Проход {pass_number}: камер={len(pass_identities)}{ping_text}\n"))
            if pass_number < SCAN_PASSES:
                time.sleep(1)

        found = self._inventory_from_vendor_rows(found_by_identity.values())
        for row in found:
            row["seen_passes"] = seen_in_passes.get(camera_identity(row), 0)
            row["online"] = True
            row["status"] = f"в сети, ответов {row['seen_passes']}/{SCAN_PASSES}"
            row["last_seen"] = dt.datetime.now().isoformat(timespec="seconds")
        found.sort(key=lambda row: (int(ipaddress.IPv4Address(row["ip"])), row["mac"]))
        self.events.put(("inventory_merge", found))

        if found:
            self.events.put(("status", f"Найдено {len(found)}. Читаю настройки камер..."))
            self._read_details_rows(found, username, password, credential_rules)
        self.events.put(("done", f"Сканирование завершено: сейчас в сети {len(found)}"))

    def _read_details_rows(self, rows: list[dict], username: str, password: str, credential_rules: list) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(rows)))) as pool:
            futures = {
                pool.submit(
                    read_camera_settings,
                    row,
                    *self._credential_for_ip(row["ip"], credential_rules, (username, password)),
                    5.0,
                ): camera_identity(row)
                for row in rows
            }
            for future in concurrent.futures.as_completed(futures):
                identity = futures[future]
                try:
                    details = future.result()
                except Exception as exc:
                    details = {"details_status": "failed", "details_message": str(exc)}
                self.events.put(("details", (identity, details)))

    def refresh_details(self) -> None:
        if self.running:
            return
        if not self._prepare_credentials():
            return
        rows = [row for row in self._visible_rows() if row.get("assign")]
        if not rows and self.table.selection():
            rows = [self.inventory[int(item)] for item in self.table.selection()]
        if not rows:
            rows = [row for row in self._visible_rows() if row.get("online")]
        if not rows:
            messagebox.showwarning("Нет доступных камер", "Сначала выполните сканирование.")
            return
        # Preserve the previous reading in history, then clear values before retrying.
        self._save_current_project()
        for row in rows:
            clear_readings(row)
            vendor = row.get("vendor_row")
            if vendor:
                vendor["mask"] = ""
                vendor["gateway"] = ""
            row["status"] = "чтение настроек"
        self._populate_table()
        self._start_worker(
            "Чтение настроек камер",
            self._details_worker,
            rows,
            self.default_username.get().strip(),
            self.default_password.get(),
            list(self.active_credential_rules),
        )

    def _details_worker(self, rows: list[dict], username: str, password: str, credential_rules: list) -> None:
        self._read_details_rows(rows, username, password, credential_rules)
        self.events.put(("done", f"Обновление данных завершено: камер {len(rows)}"))

    def _run_vendor_discovery(
        self,
        timeout: float = 6.0,
        sunell_only: bool = False,
        interface_ip: str | None = None,
    ) -> list[dict]:
        scanner = APP_DIR / "esc_vendor_bulk.py"
        output = APP_DIR / "vendor_camera_inventory_gui.csv"
        command = [
            sys.executable,
            str(scanner),
            "scan",
            "--timeout",
            str(timeout),
            "--output",
            str(output),
        ]
        if sunell_only:
            command.extend(["--skip-onvif", "--skip-dynacolor", "--repeats", "2"])
        if interface_ip is None:
            interface_ip = self.interface_ip.get().strip()
        if interface_ip:
            command.extend(["--interface-ip", interface_ip])
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if completed.stderr:
            self.events.put(("log", completed.stderr))
        if completed.returncode != 0:
            raise RuntimeError(f"esc_vendor_bulk.py завершился с кодом {completed.returncode}")
        with output.open(newline="", encoding="utf-8-sig") as stream:
            return list(csv.DictReader(stream))

    def _inventory_from_vendor_rows(self, vendor_rows) -> list[dict]:
        found = []
        for vendor_row in vendor_rows:
            ip = vendor_row.get("current_ip", "")
            if not ip:
                continue
            camera = self._camera_template_for_ip(ip)
            model = vendor_row.get("model", "")
            profile = self._profile_for_vendor_row(vendor_row, camera.get("profile", "cross"))
            camera["profile"] = profile
            found.append(
                {
                    "ip": ip,
                    "assign": False,
                    "new_ip": vendor_row.get("new_ip", ""),
                    "model": model,
                    "mac": vendor_row.get("mac", ""),
                    "device_id": vendor_row.get("device_id", ""),
                    "serial_number": vendor_row.get("serial_number", ""),
                    "mask": vendor_row.get("mask", ""),
                    "gateway_read": vendor_row.get("gateway", ""),
                    "ntp_read": "",
                    "timezone_read": "",
                    "codec": "",
                    "protocol": vendor_row.get("protocol", ""),
                    "profile": profile,
                    "status": vendor_row.get("protocol", "производитель"),
                    "seen_passes": 0,
                    "online": True,
                    "camera": camera,
                    "vendor_row": vendor_row,
                }
            )
        return found

    def _merge_inventory(self, found: list[dict]) -> None:
        # A scan is a new observation. Previous observations live only in the history DB.
        self.inventory = found
        for row in self.inventory:
            row["assign"] = False
            row["new_ip"] = ""

    @staticmethod
    def _profile_for_vendor_row(vendor_row: dict, fallback: str = "cross") -> str:
        protocol = vendor_row.get("protocol", "").lower()
        model = vendor_row.get("model", "").lower()
        if "sunell" in protocol:
            return "cross"
        if "/s8" in model or model.endswith("s8"):
            return "apix_s8"
        if "onvif" in protocol:
            return "apix_e8"
        return fallback

    def build_address_plan(self) -> None:
        if self.running:
            return
        if not self.inventory:
            messagebox.showwarning("Список пуст", "Сначала выполните сканирование.")
            return
        try:
            start = ipaddress.IPv4Address(self.target_start.get().strip())
            end_text = self.target_end.get().strip()
            if end_text:
                end = ipaddress.IPv4Address(end_text)
            else:
                last_octet_capacity = 254 - int(str(start).split(".")[-1]) + 1
                count = min(len(self.inventory), last_octet_capacity)
                end = ipaddress.IPv4Address(int(start) + count - 1)
            if int(end) < int(start):
                raise ValueError("Конечный адрес пула меньше начального.")
        except ValueError as exc:
            messagebox.showerror("Ошибка пула", str(exc))
            return
        targets = [str(ipaddress.IPv4Address(value)) for value in range(int(start), int(end) + 1)]
        selected_rows = [row for row in self._visible_rows() if row.get("assign")]
        if not selected_rows:
            messagebox.showwarning("Камеры не отмечены", "Отметьте камеры в колонке «Назначить».")
            return
        for row in self.inventory:
            if not row.get("assign"):
                row["new_ip"] = ""
                continue
            index = selected_rows.index(row)
            row["new_ip"] = targets[index] if index < len(targets) else ""
            row["status"] = "план готов" if row["new_ip"] else "пул закончился"
        self._populate_table()
        self.status.set(f"План: {min(len(targets), len(selected_rows))}/{len(selected_rows)}")

    def export_inventory(self) -> None:
        scope = self.export_scope.get()
        if scope == "Весь текущий список":
            rows = self.inventory
        elif scope == "Отмеченные строки":
            rows = [row for row in self._visible_rows() if row.get("assign")]
        else:
            rows = self._visible_rows()
        if not rows:
            messagebox.showwarning("Список пуст", "Нет строк для выбранного варианта выгрузки.")
            return
        path = filedialog.asksaveasfilename(title=f"Экспорт: {scope} ({len(rows)})",
            initialdir=str(APP_DIR), initialfile=f"Камеры_{dt.datetime.now():%Y%m%d_%H%M}.xlsx",
            defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if not path:
            return
        try:
            export_rows(path, rows, self.current_project_name, scope)
        except Exception as exc:
            messagebox.showerror("Экспорт не выполнен", str(exc))
            return
        self.status.set(f"Экспортировано строк: {len(rows)} — {pathlib.Path(path).name}")

    def apply_address_plan(self) -> None:
        if not self._prepare_credentials():
            return
        rows = [row for row in self._visible_rows() if row.get("assign") and row.get("new_ip")]
        if not rows:
            messagebox.showwarning("План не сформирован", "Сформируйте план назначения IP-адресов.")
            return
        default_rows = [row for row in rows if row.get("ip") == DEFAULT_CAMERA_IP]
        if default_rows:
            if len(default_rows) != len(rows):
                messagebox.showerror(
                    "Смешанный план",
                    "Камеры с 192.168.0.250 назначаются отдельно от камер с уникальными IP-адресами.",
                )
                return
            self.apply_default_ip_plan()
            return
        passes_text = self.pass_count.get()
        passes = None if passes_text == "без ограничения" else int(passes_text)
        preview = "\n".join(f"{row['ip']}  →  {row['new_ip']}" for row in rows[:8])
        if len(rows) > 8:
            preview += f"\n... ещё {len(rows) - 8}"
        repeat_text = "до завершения" if passes is None else str(passes)
        if not messagebox.askyesno(
            "Подтверждение смены IP",
            f"Прогонов: {repeat_text}\n\n{preview}\n\nОтправить команды смены IP?",
        ):
            return
        self._start_worker("Назначение IP", self._address_worker, rows, passes)

    def apply_default_ip_plan(self) -> None:
        if self.running:
            return
        if not self._prepare_credentials():
            return
        rows = [
            row
            for row in self._visible_rows()
            if row.get("assign") and row.get("new_ip") and row.get("ip") == DEFAULT_CAMERA_IP
        ]
        if not rows:
            messagebox.showwarning(
                "План не сформирован",
                "Отметьте камеры с 192.168.0.250 и сформируйте для них план новых адресов.",
            )
            return
        target_end_text = self.target_end.get().strip()
        if target_end_text:
            try:
                target_end = ipaddress.IPv4Address(target_end_text)
            except ipaddress.AddressValueError as exc:
                messagebox.showerror("Ошибка пула", str(exc))
                return
            last_target = max(ipaddress.IPv4Address(row["new_ip"]) for row in rows)
            template = rows[-1]
            for value in range(int(last_target) + 1, int(target_end) + 1):
                extra = copy.deepcopy(template)
                extra["new_ip"] = str(ipaddress.IPv4Address(value))
                extra["mac"] = ""
                extra["device_id"] = ""
                rows.append(extra)
        targets = [row["new_ip"] for row in rows]
        if len(targets) != len(set(targets)):
            messagebox.showerror("Ошибка плана", "В плане назначения есть повторяющиеся новые IP-адреса.")
            return
        if DEFAULT_CAMERA_IP in targets:
            messagebox.showerror("Ошибка плана", "Новый адрес не должен совпадать с 192.168.0.250.")
            return
        if not self.network_mask.get().strip() or not self.gateway.get().strip():
            messagebox.showerror("Сеть не заполнена", "Укажите маску и шлюз для новых адресов.")
            return

        preview = "\n".join(f"192.168.0.250  →  {row['new_ip']}" for row in rows[:8])
        if len(rows) > 8:
            preview += f"\n... ещё {len(rows) - 8}"
        if not messagebox.askyesno(
            "Камеры с одинаковым IP",
            f"Будет использовано адресов из пула: до {len(rows)}.\n"
            "Команда отправляется той камере на 192.168.0.250, которая ответит первой. "
            "После каждой смены программа сбросит ARP и дождётся ответа нового IP. "
            "При первой ошибке обработка остановится.\n\n"
            f"{preview}\n\nНачать?",
        ):
            return
        self._start_worker(
            "Проверка плана для 192.168.0.250",
            self._default_ip_worker,
            rows,
        )

    @staticmethod
    def _is_admin() -> bool:
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    @classmethod
    def _flush_arp(cls, ip: str) -> bool:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        success = False
        try:
            res = subprocess.run(
                ["arp", "-d", ip],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startup,
                check=False,
            )
            if res.returncode == 0:
                success = True
        except Exception:
            pass
        try:
            subprocess.run(
                ["netsh", "interface", "ip", "delete", "arpcache"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startup,
                check=False,
            )
        except Exception:
            pass
        return success

    @staticmethod
    def _vendor_runtime_paths() -> tuple[pathlib.Path, pathlib.Path]:
        bridge = APP_DIR / "vendor_bridge" / "VendorBridge.exe"
        portable_vendor = APP_DIR / "vendor"
        configured = os.environ.get("ESC_VENDOR_DIR", "").strip()
        vendor_dir = portable_vendor if portable_vendor.is_dir() else pathlib.Path(
            configured or r"C:\Program Files (x86)\ESC\lib\Starter"
        )
        return bridge, vendor_dir

    def _send_targeted_sunell_ip(
        self,
        row: dict,
        target_ip: str,
        username: str,
        password: str,
        mask: str,
        gateway: str,
        dns: str,
    ) -> tuple[bool, str]:
        bridge, vendor_dir = self._vendor_runtime_paths()
        if not bridge.exists():
            return False, f"Не найден фирменный мост: {bridge}"
        if not vendor_dir.is_dir():
            return False, f"Не найдены DLL производителя: {vendor_dir}"
        command = [
            str(bridge),
            "set-sunell",
            DEFAULT_CAMERA_IP,
            target_ip,
            mask,
            gateway,
            dns,
            row.get("model", ""),
            row.get("mac", ""),
            row.get("device_id", ""),
            username,
        ]
        environment = dict(os.environ)
        environment["ESC_VENDOR_DIR"] = str(vendor_dir)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            process = subprocess.run(
                command,
                input=password + "\n",
                capture_output=True,
                text=True,
                timeout=30,
                cwd=vendor_dir,
                env=environment,
                startupinfo=startup,
            )
        except subprocess.SubprocessError as exc:
            return False, str(exc)
        message = (process.stderr or process.stdout).strip()
        return process.returncode == 0, message

    def _identity_at_ip(
        self,
        expected_row: dict,
        target_ip: str,
        interface_ip: str,
        timeout: float = 50.0,
    ) -> tuple[bool, str]:
        expected = camera_identity(expected_row)
        deadline = time.monotonic() + timeout
        last_message = "новый адрес не отвечает"
        while time.monotonic() < deadline:
            self._flush_arp(target_ip)
            if not self._host_replies(target_ip):
                time.sleep(2)
                continue
            last_message = "IP отвечает, но MAC/DeviceID еще не подтвержден"
            try:
                discovered = self._run_vendor_discovery(
                    timeout=2.5,
                    sunell_only=True,
                    interface_ip=interface_ip,
                )
            except Exception as exc:
                last_message = f"ошибка проверки производителя: {exc}"
                time.sleep(2)
                continue
            for item in discovered:
                if item.get("current_ip") == target_ip and camera_identity(item) == expected:
                    return True, "MAC/DeviceID подтвержден"
            time.sleep(2)
        return False, last_message

    def _discover_sunell_union(self, interface_ip: str, passes: int = 3) -> list[dict]:
        found = {}
        for pass_number in range(1, passes + 1):
            self.events.put(("status", f"Проверка исходных камер: проход {pass_number}/{passes}"))
            rows = self._run_vendor_discovery(
                timeout=3.0,
                sunell_only=True,
                interface_ip=interface_ip,
            )
            for row in rows:
                found[camera_identity(row)] = row
            if pass_number < passes:
                time.sleep(1)
        return list(found.values())

    def _write_default_assignment_log(self, results: list[dict]) -> None:
        fields = ["timestamp", "mac", "device_id", "source_ip", "target_ip", "status", "message"]
        write_header = not DEFAULT_ASSIGNMENT_LOG.exists() or DEFAULT_ASSIGNMENT_LOG.stat().st_size == 0
        with DEFAULT_ASSIGNMENT_LOG.open("a", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            if write_header:
                writer.writeheader()
            writer.writerows(results)

    def _default_ip_worker(
        self,
        rows: list[dict],
    ) -> None:
        results = []
        targets = [row["new_ip"] for row in rows]
        occupied = self._check_target_pool(targets)
        if self.cancel_requested.is_set():
            self.events.put(("done", "Проверка плана остановлена; команды смены IP не отправлялись"))
            return
        skipped = len(occupied)
        if occupied:
            self.events.put(("log", "Пропускаю занятые адреса: " + ", ".join(occupied) + "\n"))
            occupied_set = set(occupied)
            available = []
            for row in rows:
                if row["new_ip"] not in occupied_set:
                    available.append(row)
                    continue
                results.append({"timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                                "mac": "", "device_id": "", "source_ip": DEFAULT_CAMERA_IP,
                                "target_ip": row["new_ip"], "status": "skipped_occupied",
                                "message": "Адрес занят; команда не отправлялась"})
                row.update(status="целевой адрес занят — пропущен", assign=False, new_ip="")
            rows = available
            self.events.put(("refresh", None))
        if not rows:
            self._write_default_assignment_log(results)
            self.events.put(("done", f"В заданном пуле нет свободных адресов; пропущено {skipped}"))
            return
        self.events.put(("log", f"Свободных адресов: {len(rows)}; занятых пропущено: {skipped}\n"))

        if not self._is_admin():
            self.events.put(("log", "⚠ Внимание: программа запущена без прав администратора. Сброс ARP-кэша Windows может быть заблокирован. Рекомендуется запускать через camera-gui.cmd от имени Администратора.\n"))

        completed = 0
        self._last_default_profile = None
        writer = QueueWriter(self.events)
        self.events.put(("progress", (0, len(rows))))
        for index, row in enumerate(rows, start=1):
            if self.cancel_requested.is_set():
                break
            target_ip = row["new_ip"]
            result = {
                "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                "mac": row.get("mac", ""),
                "device_id": row.get("device_id", ""),
                "source_ip": DEFAULT_CAMERA_IP,
                "target_ip": target_ip,
                "status": "failed",
                "message": "",
            }
            mac_label = row.get("mac") or "неизвестен"
            self.events.put(("status", f"Камера {index}/{len(rows)}: 192.168.0.250 → {target_ip}"))
            self.events.put(("log", f"[{index}/{len(rows)}] 192.168.0.250 → {target_ip} (MAC: {mac_label})\n"))
            self._flush_arp(DEFAULT_CAMERA_IP)
            self._flush_arp(target_ip)

            with redirect_stdout(writer), redirect_stderr(writer):
                sent = self._apply_default_network_row(row)

            if not sent:
                if row.get("status") == "целевой IP занят":
                    skipped += 1
                    result.update(status="skipped_occupied", message="Адрес оказался занят перед отправкой; пропущен")
                    results.append(result)
                    row.update(status="целевой адрес занят — пропущен", assign=False, new_ip="")
                    self.events.put(("log", f"{target_ip} оказался занят; пропускаю\n"))
                    self.events.put(("progress", (index, len(rows))))
                    self.events.put(("refresh", None))
                    continue
                result["message"] = row.get("status", "команда отклонена")
                results.append(result)
                self.events.put(("log", f"  ✗ Не удалось отправить смену IP: {row.get('status')}\n"))
                self.events.put(("progress", (index, len(rows))))
                self.events.put(("refresh", None))
                continue

            # Команда успешно отправлена
            completed += 1
            row["ip"] = target_ip
            row["new_ip"] = ""
            row["online"] = False
            row["status"] = "команда отправлена"
            row["last_seen"] = dt.datetime.now().isoformat(timespec="seconds")
            row["camera"]["ip"] = target_ip
            if row.get("vendor_row"):
                row["vendor_row"]["current_ip"] = target_ip

            result["status"] = "sent"
            result["message"] = "команда смены IP успешно отправлена"
            results.append(result)

            self.events.put(("log", f"  ✓ Команда для {target_ip} отправлена. Сброс ARP и пауза 3с...\n"))
            self._flush_arp(DEFAULT_CAMERA_IP)
            self._flush_arp(target_ip)
            self.events.put(("progress", (index, len(rows))))
            self.events.put(("refresh", None))

            # Технологическая пауза для переключения камеры и сброса коммутатора
            self.cancel_requested.wait(3)

        self._write_default_assignment_log(results)

        # Быстрая проверка доступности новых адресов в сети (не блокирует конвейер)
        if completed > 0 and not self.cancel_requested.is_set():
            self.events.put(("status", "Проверка доступности назначенных адресов в сети..."))
            self.events.put(("log", "\nПроверка ответов новых IP-адресов...\n"))
            for r in rows:
                if r.get("status") == "команда отправлена" and r.get("ip"):
                    check_ip = r["ip"]
                    self._flush_arp(check_ip)
                    if self._host_replies(check_ip):
                        r["status"] = "новый IP отвечает"
                        r["online"] = True
                        self.events.put(("log", f"  ✓ {check_ip} онлайн\n"))
                    else:
                        r["status"] = "отправлено (перезагрузка)"
            self.events.put(("refresh", None))

        if self.cancel_requested.is_set():
            message = f"Остановлено: отправлено {completed}/{len(rows)}; новые команды не отправляются"
        else:
            message = f"Назначение 192.168.0.250: отправлено {completed}/{len(rows)}"
        if skipped:
            message += f"; занятых адресов пропущено: {skipped}"
        self.events.put(("done", message))

    def _request_stop(self):
        self.cancel_requested.set()
        self.stop_button.configure(state="disabled")
        self.status.set("Остановка: завершаю текущий запрос; отправленную смену IP сначала проверю")

    def _check_target_pool(self, targets):
        """Bound concurrent checks and publish progress before the first write."""
        occupied = []
        total = len(targets)
        self.events.put(("progress", (0, total)))
        self.events.put(("status", f"Проверка новых адресов: 0/{total}"))
        self.events.put(("log", f"Проверяю {total} новых адресов, до 8 одновременно…\n"))
        def check(ip):
            if self.cancel_requested.is_set():
                return False
            self._flush_arp(ip)
            return self._host_replies(ip)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            pending = {pool.submit(check, ip): ip for ip in targets}
            for count, future in enumerate(concurrent.futures.as_completed(pending), 1):
                ip = pending[future]
                if future.result():
                    occupied.append(ip)
                self.events.put(("progress", (count, total)))
                self.events.put(("status", f"Проверка новых адресов: {count}/{total}; занято: {len(occupied)}"))
                if self.cancel_requested.is_set():
                    for task in pending:
                        task.cancel()
                    break
        return occupied

    def _wait_for_default_ip(self, max_wait: float = 120.0) -> bool:
        """Wait until DEFAULT_CAMERA_IP responds to ping, allowing cameras to boot or switch to update ARP."""
        start = time.monotonic()
        while not self.cancel_requested.is_set():
            self._flush_arp(DEFAULT_CAMERA_IP)
            if self._host_replies(DEFAULT_CAMERA_IP):
                return True
            elapsed = int(time.monotonic() - start)
            if max_wait > 0 and elapsed >= max_wait:
                return False
            self.events.put(("status", f"Ожидание камеры на {DEFAULT_CAMERA_IP}... ({elapsed}с, нажмите «Остановить» для завершения)"))
            self.cancel_requested.wait(2.0)
        return False

    def _apply_default_network_row(self, row):
        """Try supported methods on the default IP; prioritize targeted vendor protocol."""
        target = row["new_ip"]
        if self.cancel_requested.is_set():
            return False
        if self._host_replies(target):
            row["status"] = "целевой IP занят"
            return False

        config = self._runtime_config()
        config.setdefault("network", {})["ip_address"] = target

        # 1. Приоритетный способ: если камера обнаружена по протоколу Sunell с MAC/DeviceID
        if (not self.cancel_requested.is_set() and row.get("protocol") == "sunell"
                and row.get("mac") and row.get("device_id")):
            tz_val = config.get("timezone", {}).get("timezone")
            ntp_val = config.get("ntp", {}).get("server")
            if tz_val or ntp_val:
                try:
                    sunell_cam = copy.deepcopy(row["camera"])
                    sunell_cam.update(ip=DEFAULT_CAMERA_IP, profile="cross")
                    sunell_drv = self._make_driver(sunell_cam, config)
                    if sunell_drv.connect():
                        if tz_val:
                            sunell_drv.apply_timezone()
                        if ntp_val:
                            sunell_drv.apply_ntp()
                except Exception:
                    pass
            self.events.put(("status", f"{DEFAULT_CAMERA_IP} → {target}: фирменный метод Sunell (MAC {row.get('mac')})"))
            print(f"  Приоритетный способ: фирменный метод Sunell по MAC {row.get('mac')}")
            username, password = self._credential_for_ip(DEFAULT_CAMERA_IP, self.active_credential_rules,
                                                        (self.default_username.get().strip(), self.default_password.get()))
            network = config["network"]
            sent, message = self._send_targeted_sunell_ip(row, target, username, password,
                network["subnet_mask"], network["gateway"], network.get("dns_main", ""))
            if sent:
                row["status"] = "фирменная команда отправлена"
                print(f"  ✓ Фирменная команда успешно отправлена на {row.get('mac')}")
                return True
            print(f"  Фирменный метод вернул ошибку ({message}); пробую веб-методы...")

        # 2. Перебор HTTP-методов (CROSS, LAPI, CGI)
        model = str(row.get("model", "")).lower()
        last_success = getattr(self, "_last_default_profile", None)
        preferred = last_success or ("apix_s8" if "/s8" in model else "apix_e8" if "/e8" in model else row.get("profile", "cross"))
        profiles = list(dict.fromkeys([preferred, "apix_e8", "cross", "apix_s8"]))
        profiles = [p for p in profiles if p in {"cross", "apix_e8", "apix_s8"}]
        labels = {"cross": "CROSS / WEB", "apix_e8": "APIX E8 / LAPI", "apix_s8": "APIX S8 / CGI"}

        for index, profile in enumerate(profiles, 1):
            if self.cancel_requested.is_set():
                row["status"] = "остановлено до смены IP"
                return False
            label = labels[profile]
            self.events.put(("status", f"{DEFAULT_CAMERA_IP} → {target}: способ {index}/{len(profiles)} — {label}"))
            print(f"  Способ {index}/{len(profiles)}: {label}")
            camera = copy.deepcopy(row["camera"])
            camera.update(ip=DEFAULT_CAMERA_IP, profile=profile, network_autodetect=True)
            driver = self._make_driver(camera, config)
            try:
                connected = driver.connect()
            except Exception as exc:
                print(f"  Подключение {label}: {type(exc).__name__}")
                continue
            if not connected or getattr(driver, "onvif_fallback", False):
                print(f"  {label}: способ настройки сети не подтверждён; пробую следующий")
                continue
            if self.cancel_requested.is_set():
                row["status"] = "остановлено до смены IP"
                return False

            # Настройка часового пояса и NTP перед сменой сети
            tz_val = config.get("timezone", {}).get("timezone")
            if tz_val:
                try:
                    self.events.put(("status", f"{DEFAULT_CAMERA_IP} → {target}: настройка часового пояса ({label})"))
                    tz_ok = driver.apply_timezone()
                    print(f"  Часовой пояс ({tz_val}): {'✓' if tz_ok else '✗'}")
                except Exception as exc:
                    print(f"  Часовой пояс: {exc}")

            ntp_val = config.get("ntp", {}).get("server")
            if ntp_val:
                try:
                    self.events.put(("status", f"{DEFAULT_CAMERA_IP} → {target}: настройка NTP-сервера ({label})"))
                    ntp_ok = driver.apply_ntp()
                    print(f"  NTP-сервер ({ntp_val}): {'✓' if ntp_ok else '✗'}")
                except Exception as exc:
                    print(f"  NTP-сервер: {exc}")

            self.events.put(("status", f"{DEFAULT_CAMERA_IP} → {target}: отправка сети через {label}"))
            try:
                sent = driver.apply_network()
            except configurator.requests.RequestException as exc:
                response = getattr(exc, "response", None)
                code = response.status_code if response is not None else None
                if code in {400, 401, 403, 404, 405, 501}:
                    print(f"  {label}: HTTP {code}; пробую следующий способ")
                    continue
                row["status"] = f"{label}: команда отправлена"
                print(f"  {label}: соединение сброшено после отправки (камера меняет IP)")
                self._last_default_profile = profile
                return True
            if sent:
                self._last_default_profile = profile
                row["profile"] = profile
                row["camera"]["profile"] = profile
                row["status"] = f"команда отправлена: {label}"
                return True
            code = getattr(driver, "network_http_status", None)
            if code is not None and code >= 500 and code != 501:
                row["status"] = f"{label}: команда отправлена"
                self._last_default_profile = profile
                return True
            print(f"  {label}: команда отклонена; пробую следующий способ")

        row["status"] = "остановлено" if self.cancel_requested.is_set() else "ни один способ настройки сети не подошёл"
        print(f"{DEFAULT_CAMERA_IP} -> {target}: {row['status']}")
        return False

    def _apply_network_row(self, row: dict) -> bool:
        source_ip = row["ip"]
        target_ip = row["new_ip"]
        if source_ip != target_ip and self._host_replies(target_ip):
            row["status"] = "целевой IP занят"
            print(f"{source_ip} -> {target_ip}: целевой IP занят")
            return False
        camera = copy.deepcopy(row["camera"])
        camera["ip"] = source_ip
        config_data = self._runtime_config()
        config_data.setdefault("network", {})["ip_address"] = target_ip
        driver = self._make_driver(camera, config_data)
        if not driver.connect():
            row["status"] = "нет подключения"
            print(f"{source_ip} -> {target_ip}: нет подключения")
            return False
        tz_val = config_data.get("timezone", {}).get("timezone")
        if tz_val:
            try:
                driver.apply_timezone()
            except Exception as exc:
                print(f"  Часовой пояс: {exc}")
        ntp_val = config_data.get("ntp", {}).get("server")
        if ntp_val:
            try:
                driver.apply_ntp()
            except Exception as exc:
                print(f"  NTP-сервер: {exc}")
        if not driver.apply_network():
            row["status"] = "команда отклонена"
            print(f"{source_ip} -> {target_ip}: команда отклонена")
            return False
        row["status"] = "команда отправлена"
        print(f"{source_ip} -> {target_ip}: команда отправлена")
        return True

    def _address_worker(self, initial_rows: list[dict], passes: int | None) -> None:
        writer = QueueWriter(self.events)
        source_start = ipaddress.IPv4Address(self.scan_start.get().strip())
        source_end_text = self.scan_end.get().strip()
        source_end = ipaddress.IPv4Address(source_end_text) if source_end_text else source_start
        target_start = ipaddress.IPv4Address(self.target_start.get().strip())
        target_end_text = self.target_end.get().strip()
        target_end = ipaddress.IPv4Address(target_end_text) if target_end_text else ipaddress.IPv4Address(
            (int(target_start) & ~255) + 254
        )
        used_targets = {row["new_ip"] for row in initial_rows}
        next_target_value = max(int(ipaddress.IPv4Address(value)) for value in used_targets) + 1
        pending_rows = initial_rows
        pass_number = 0
        successful = 0

        with redirect_stdout(writer), redirect_stderr(writer):
            while pending_rows and (passes is None or pass_number < passes):
                pass_number += 1
                print(f"\nПрогон {pass_number}: камер {len(pending_rows)}")
                pass_success = 0
                for row in pending_rows:
                    try:
                        if self._apply_network_row(row):
                            pass_success += 1
                            successful += 1
                    except Exception as exc:
                        row["status"] = f"ошибка: {exc}"
                        print(f"{row['ip']} -> {row['new_ip']}: {exc}")
                self.events.put(("refresh", None))

                if passes is not None and pass_number >= passes:
                    break
                if pass_success == 0:
                    print("Нет успешных команд; повторение остановлено.")
                    break

                print("Ожидание 15 секунд перед повторным поиском дублей...")
                time.sleep(15)
                source_ips = [
                    str(ipaddress.IPv4Address(value))
                    for value in range(int(source_start), int(source_end) + 1)
                ]
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, max(1, len(source_ips)))) as pool:
                    remaining_sources = [ip for ip, alive in zip(source_ips, pool.map(self._host_replies, source_ips)) if alive]
                if not remaining_sources:
                    print("Исходный диапазон пуст; дубли не обнаружены.")
                    break

                pending_rows = []
                for source_ip in remaining_sources:
                    while next_target_value <= int(target_end):
                        candidate = str(ipaddress.IPv4Address(next_target_value))
                        next_target_value += 1
                        if candidate not in used_targets:
                            break
                    else:
                        print("Пул назначения закончился.")
                        pending_rows = []
                        break
                    used_targets.add(candidate)
                    source_row = next((row for row in self.inventory if row.get("ip") == source_ip), None)
                    camera = copy.deepcopy(source_row["camera"]) if source_row else self._camera_template_for_ip(source_ip)
                    row = {
                        "ip": source_ip,
                        "assign": True,
                        "new_ip": candidate,
                        "model": source_row.get("model", "") if source_row else camera.get("model", ""),
                        "mac": source_row.get("mac", "") if source_row else camera.get("mac", ""),
                        "profile": camera.get("profile", "cross"),
                        "status": "повторный прогон",
                        "camera": camera,
                        "vendor_row": None,
                    }
                    self.inventory.append(row)
                    pending_rows.append(row)
                self.events.put(("refresh", None))

        self.events.put(("done", f"Команд смены IP выполнено: {successful}"))

    def _selected_operations(self) -> list[tuple[str, str, str]]:
        return [operation for operation in OPERATIONS if self.operation_vars[operation[0]].get()]

    def test_selected(self) -> None:
        if not self._prepare_credentials():
            return
        cameras = self.selected_cameras()
        if not cameras:
            messagebox.showwarning("Камеры не выбраны", "Выберите хотя бы одну камеру.")
            return
        self._start_worker("Проверка подключения", self._test_worker, cameras)

    def apply_selected(self) -> None:
        if not self._prepare_credentials():
            return
        cameras = self.selected_cameras()
        operations = self._selected_operations()
        if not cameras:
            messagebox.showwarning("Камеры не выбраны", "Выберите хотя бы одну камеру.")
            return
        if not operations:
            messagebox.showwarning("Разделы не выбраны", "Выберите хотя бы один раздел настроек.")
            return
        if any(key == "network" for key, _label, _method in operations) and len(cameras) > 1:
            messagebox.showerror(
                "Массовая смена сети заблокирована",
                "В YAML задан один общий IP-адрес. Для смены сети выберите только одну камеру.",
            )
            return
        labels = ", ".join(label for _key, label, _method in operations)
        confirmation_values = []
        if any(key == "ntp" for key, _label, _method in operations):
            confirmation_values.append(f"NTP-сервер: {self.ntp_server.get().strip() or 'НЕ ЗАДАН'}")
        if any(key == "timezone" for key, _label, _method in operations):
            confirmation_values.append(f"Часовой пояс: {self.cross_timezone.get().strip() or 'НЕ ЗАДАН'}")
        values_text = "\n".join(confirmation_values)
        if not messagebox.askyesno(
            "Подтверждение",
            f"Применить разделы «{labels}» к камерам: {len(cameras)}?\n\n{values_text}",
        ):
            return
        self._start_worker("Применение настроек", self._apply_worker, cameras, operations)

    def _runtime_config(self) -> dict:
        data = copy.deepcopy(self.config_data)
        ntp_srv = self.ntp_server.get().strip()
        data.setdefault("ntp", {})["server"] = ntp_srv
        data.setdefault("ntp", {})["enabled"] = bool(ntp_srv)
        timezone_text = self.cross_timezone.get().strip()
        timezone = data.setdefault("timezone", {})
        timezone["timezone"] = timezone_text
        match = re.search(r"GMT([+-])(\d{2}):(\d{2})", timezone_text)
        if match:
            sign, hours, minutes = match.groups()
            offset = f"{sign}{hours}:{minutes}"
            timezone["timezone_offset"] = offset
            timezone["timezone_lapi"] = f"GMT{offset}"
            # Для формата POSIX TZ (S8) знак инвертируется: восточнее GMT пишется с минусом (GMT+9 -> UTC-9, GMT+3 -> UTC-3)
            posix_sign = "-" if sign == "+" else "+"
            tz_min = f":{minutes}" if minutes != "00" else ""
            timezone["timezone_utc"] = f"UTC{posix_sign}{int(hours)}{tz_min}"
            timezone["timezone_name"] = "RUS"
        network = data.setdefault("network", {})
        network["subnet_mask"] = self.network_mask.get().strip()
        network["gateway"] = self.gateway.get().strip()
        network["dns_main"] = self.dns_main.get().strip()
        return data

    def _make_driver(self, camera: dict, config_data: dict):
        camera = copy.deepcopy(camera)
        username, password = self._credential_for_ip(
            camera["ip"],
            self.active_credential_rules,
            (self.default_username.get().strip(), self.default_password.get()),
        )
        camera["username"] = username
        camera["password"] = password
        profile_name = camera.get("profile", "cross")
        profile = configurator.load_profile(profile_name)
        driver_class = configurator.DRIVERS.get(profile.get("driver"))
        if driver_class is None:
            raise ValueError(f"Неизвестный драйвер: {profile.get('driver')}")
        return driver_class(profile, camera, config_data)

    def _test_worker(self, cameras: list[dict]) -> None:
        config_data = self._runtime_config()
        success = 0
        writer = QueueWriter(self.events)
        with redirect_stdout(writer), redirect_stderr(writer):
            for camera in cameras:
                try:
                    print(f"\n{camera['ip']}: проверка {camera.get('profile', 'cross')}")
                    driver = self._make_driver(camera, config_data)
                    if driver.connect():
                        success += 1
                        print("  OK")
                    else:
                        print("  ОШИБКА")
                except Exception as exc:
                    print(f"  ОШИБКА: {exc}")
        self.events.put(("done", f"Подключение: {success}/{len(cameras)}"))

    def _apply_worker(self, cameras: list[dict], operations: list[tuple[str, str, str]]) -> None:
        config_data = self._runtime_config()
        success = 0
        writer = QueueWriter(self.events)
        with redirect_stdout(writer), redirect_stderr(writer):
            for camera in cameras:
                camera_ok = True
                try:
                    print(f"\n{camera['ip']}: применение {camera.get('profile', 'cross')}")
                    driver = self._make_driver(camera, config_data)
                    if not driver.connect():
                        print("  Подключение не выполнено")
                        continue
                    for _key, label, method_name in operations:
                        print(f"  {label}...")
                        if not bool(getattr(driver, method_name)()):
                            camera_ok = False
                    if camera_ok:
                        success += 1
                except Exception as exc:
                    camera_ok = False
                    print(f"  ОШИБКА: {exc}")
                print("  OK" if camera_ok else "  НЕ ВЫПОЛНЕНО")
        self.events.put(("done", f"Применено успешно: {success}/{len(cameras)}"))

    def _start_worker(self, status: str, target, *args) -> None:
        if self.running:
            return
        self.running = True
        self.cancel_requested.clear()
        self.status.set(status)
        self.test_button.configure(state="disabled")
        self.details_button.configure(state="disabled")
        self.apply_button.configure(state="disabled")
        self.address_button.configure(state="disabled")
        self.default_ip_button.configure(state="disabled")
        self.scan_button.configure(state="disabled")
        self._lock_controls(True)
        self.stop_button.configure(state="normal" if target.__name__ == "_default_ip_worker" else "disabled")
        def worker():
            try:
                target(*args)
            except Exception:
                # Always restore controls, even if discovery fails unexpectedly.
                self.events.put(("error", "Операция прервана ошибкой. Проверьте параметры подключения и повторите."))
                self.events.put(("done", "Операция не завершена"))
        threading.Thread(target=worker, daemon=True).start()

    def _drain_events(self) -> None:
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", value)
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif event == "done":
                    self.running = False
                    self._lock_controls(False)
                    self.stop_button.configure(state="disabled")
                    online = sum(1 for row in self.inventory if row.get("online"))
                    self.status.set(f"{value}. Известно: {len(self.inventory)}, в сети: {online}")
                    self.test_button.configure(state="normal")
                    self.details_button.configure(state="normal")
                    self.apply_button.configure(state="normal")
                    self.address_button.configure(state="normal")
                    self.default_ip_button.configure(state="normal")
                    self.scan_button.configure(state="normal")
                    self._save_current_project()
                elif event == "inventory":
                    self.inventory = value
                    self._populate_table()
                elif event == "inventory_merge":
                    self._merge_inventory(value)
                    self._populate_table()
                    self._save_current_project()
                elif event == "details":
                    identity, details = value
                    row = next((item for item in self.inventory if camera_identity(item) == identity), None)
                    if row is not None:
                        row.update(details)
                        row["details_read_at"] = dt.datetime.now().isoformat(timespec="seconds")
                        if details.get("details_status") == "failed":
                            row["status"] = "в сети; настройки не прочитаны"
                        elif details.get("details_status") == "partial":
                            row["status"] = "в сети; данные прочитаны частично"
                        else:
                            row["status"] = "в сети; настройки прочитаны"
                        self._populate_table()
                elif event == "refresh":
                    self._populate_table()
                elif event == "status":
                    self.status.set(value)
                elif event == "progress":
                    current, total = value
                    self.progress_bar.configure(maximum=max(total, 1), value=current)
                elif event == "error":
                    messagebox.showerror("Операция не выполнена", value)
                elif event == "video_ready":
                    self.video_pending = False
                    vlc, uri, username, password = value
                    try:
                        launch_video(vlc, uri, username, password)
                        self.status.set("Видеопоток открыт в VLC")
                    except Exception:
                        messagebox.showerror("Видео", "Не удалось запустить VLC.")
                elif event == "video_failed":
                    self.video_pending = False
                    vlc, ip, username, password = value
                    uri = simpledialog.askstring("Видео", "Не удалось получить поток через ONVIF.\nПроверьте доступ к камере. Можно указать известный RTSP-адрес вручную:", parent=self)
                    if uri:
                        try:
                            uri = camera_url(uri.strip(), ip, {"rtsp", "rtsps"})
                            launch_video(vlc, uri, username, password)
                        except Exception:
                            messagebox.showerror("Видео", "Не удалось открыть RTSP-адрес в VLC.")
        except queue.Empty:
            pass
        self.after(100, self._drain_events)


def main() -> int:
    app = CameraGui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
