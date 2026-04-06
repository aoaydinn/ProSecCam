#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""
ProSecCam v2.0 - Professional Motion-Detection Security Camera for Termux

Features:
  - OpenCV motion detection with adaptive calibration
  - 60s minimum video recording (photo burst + audio -> MP4)
  - 15s cooldown between recordings
  - Automatic old file cleanup (age + size based)
  - Battery & temperature monitoring with adaptive behavior
  - Telegram / SMS / termux-notification alerts
  - Night mode with torch control
  - Auto-installer for first run
  - Graceful shutdown with full resource cleanup

Requirements:
  - Termux + Termux:API (both from F-Droid)
  - Android permissions: Camera, Microphone, Storage (SMS optional)

Usage:
  python ProSecCam.py --setup          # First-time auto setup
  python ProSecCam.py --init-config    # Generate config file
  python ProSecCam.py --dry-run        # Test without recording
  python ProSecCam.py                  # Start security camera
"""

__version__ = "2.0.0"

import argparse
import atexit
import enum
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Callable, Dict, List, Optional, Tuple

# ==========================================================================
# AUTO-INSTALLER & DEPENDENCY CHECKER
# ==========================================================================

class TermuxSetup:
    """Handles first-run setup: package installation, permission checks,
    dependency verification, and phantom process killer guidance."""

    TERMUX_HOME = "/data/data/com.termux/files/home"
    TERMUX_PREFIX = "/data/data/com.termux/files/usr"

    # Packages to install via pkg
    REQUIRED_PACKAGES = [
        "termux-api",
        "python",
        "ffmpeg",
        "git",
        "curl",
    ]

    # Build dependencies for numpy/opencv
    BUILD_PACKAGES = [
        "build-essential",
        "cmake",
        "ninja",
        "clang",
        "make",
        "ndk-sysroot",
        "patchelf",
        "binutils-is-llvm",
        "libjpeg-turbo",
        "libpng",
        "zlib",
        "freetype",
        "libopenblas",
        "libandroid-execinfo",
    ]

    # Termux:API commands that must be available
    REQUIRED_API_COMMANDS = [
        "termux-camera-photo",
        "termux-microphone-record",
        "termux-battery-status",
        "termux-wake-lock",
        "termux-wake-unlock",
        "termux-notification",
        "termux-torch",
    ]

    OPTIONAL_API_COMMANDS = [
        "termux-sms-send",
        "termux-telephony-call",
        "termux-vibrate",
        "termux-tts-speak",
        "termux-camera-info",
    ]

    @staticmethod
    def is_termux() -> bool:
        return os.path.isdir("/data/data/com.termux")

    @classmethod
    def run_setup(cls) -> bool:
        """Full automated setup. Returns True on success."""
        print("=" * 60)
        print(f"  ProSecCam v{__version__} - Otomatik Kurulum")
        print("=" * 60)

        if not cls.is_termux():
            print("\n[!] Bu script Termux ortaminda calistirilmalidir!")
            print("    Termux'u F-Droid'den yukleyin (Google Play degil).")
            print("    Termux:API uygulamasini da F-Droid'den yukleyin.")
            return False

        steps = [
            ("Paket deposu guncelleniyor", cls._update_packages),
            ("Temel paketler yukleniyor", cls._install_base_packages),
            ("Derleme bagimliliklari yukleniyor", cls._install_build_deps),
            ("Python paketleri yukleniyor", cls._install_python_deps),
            ("Depolama izni ayarlaniyor", cls._setup_storage),
            ("Termux:API kontrol ediliyor", cls._check_api),
            ("Android izinleri kontrol ediliyor", cls._check_permissions),
            ("Phantom process killer uyarisi", cls._phantom_process_guide),
            ("Pil optimizasyonu uyarisi", cls._battery_optimization_guide),
        ]

        total = len(steps)
        for idx, (desc, func) in enumerate(steps, 1):
            print(f"\n[{idx}/{total}] {desc}...")
            try:
                func()
                print(f"  [OK] {desc} tamamlandi.")
            except Exception as e:
                print(f"  [UYARI] {desc} basarisiz: {e}")
                print("  Devam ediliyor...")

        # Final verification
        print("\n" + "=" * 60)
        print("  KURULUM DOGRULAMASI")
        print("=" * 60)
        ok = cls._verify_all()

        if ok:
            print("\n[BASARILI] Tum bagimliliklar hazir!")
            print("\nKullanim:")
            print("  python ProSecCam.py --init-config   # Config dosyasi olustur")
            print("  python ProSecCam.py --dry-run       # Test modu")
            print("  python ProSecCam.py                 # Basla")
        else:
            print("\n[UYARI] Bazi bagimliliklar eksik. Yukaridaki uyarilara bakin.")

        return ok

    @classmethod
    def _run_cmd(cls, cmd: str, timeout: int = 300) -> Tuple[int, str]:
        """Run a shell command, return (returncode, output)."""
        try:
            result = subprocess.run(
                cmd, shell=True, timeout=timeout,
                capture_output=True, text=True,
            )
            return result.returncode, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return -1, "Timeout"
        except Exception as e:
            return -1, str(e)

    @classmethod
    def _update_packages(cls) -> None:
        code, out = cls._run_cmd("pkg update -y && pkg upgrade -y", timeout=600)
        if code != 0:
            raise RuntimeError(f"pkg update failed: {out[:200]}")

    @classmethod
    def _install_base_packages(cls) -> None:
        pkgs = " ".join(cls.REQUIRED_PACKAGES)
        code, out = cls._run_cmd(f"pkg install -y {pkgs}", timeout=600)
        if code != 0:
            raise RuntimeError(f"Package install failed: {out[:200]}")

    @classmethod
    def _install_build_deps(cls) -> None:
        pkgs = " ".join(cls.BUILD_PACKAGES)
        code, out = cls._run_cmd(f"pkg install -y {pkgs}", timeout=600)
        if code != 0:
            print(f"  [UYARI] Bazi derleme paketleri yuklenemedi: {out[:200]}")

    @classmethod
    def _install_python_deps(cls) -> None:
        # numpy - try pkg first (prebuilt), fallback to pip
        print("  numpy yukleniyor...")
        code, _ = cls._run_cmd("pkg install -y python-numpy", timeout=300)
        if code != 0:
            print("  pkg python-numpy bulunamadi, pip ile deneniyor...")
            py_ver = cls._get_python_version()
            code, out = cls._run_cmd(
                f'MATHLIB=m LDFLAGS="-lpython{py_ver}" '
                f'pip install --no-build-isolation --no-cache-dir numpy',
                timeout=600,
            )
            if code != 0:
                raise RuntimeError(f"numpy install failed: {out[:200]}")

        # Pillow
        print("  Pillow yukleniyor...")
        code, out = cls._run_cmd("pip install Pillow", timeout=300)
        if code != 0:
            # Fallback with env vars
            cls._run_cmd(
                'env INCLUDE="$PREFIX/include" LDFLAGS=" -lm" pip install Pillow',
                timeout=300,
            )

        # OpenCV - try pkg first, then pip headless
        print("  OpenCV yukleniyor (bu uzun surebilir)...")
        code, _ = cls._run_cmd("pkg install -y opencv", timeout=300)
        if code != 0:
            print("  pkg opencv bulunamadi, pip ile deneniyor...")
            code, out = cls._run_cmd(
                "pip install --no-build-isolation --no-cache-dir "
                "opencv-python-headless",
                timeout=1200,  # Can take 20+ minutes
            )
            if code != 0:
                raise RuntimeError(
                    f"OpenCV install failed: {out[:200]}\n"
                    "Manuel kurulum deneyin: pkg install opencv"
                )

    @classmethod
    def _get_python_version(cls) -> str:
        """Get python version like '3.12'."""
        return f"{sys.version_info.major}.{sys.version_info.minor}"

    @classmethod
    def _setup_storage(cls) -> None:
        code, _ = cls._run_cmd("termux-setup-storage", timeout=30)
        # This opens a permission dialog, may not return cleanly
        time.sleep(2)

    @classmethod
    def _check_api(cls) -> None:
        missing = []
        for cmd in cls.REQUIRED_API_COMMANDS:
            code, _ = cls._run_cmd(f"command -v {cmd}")
            if code != 0:
                missing.append(cmd)

        if missing:
            print(f"  [UYARI] Eksik API komutlari: {', '.join(missing)}")
            print("  Cozum: Termux:API uygulamasini F-Droid'den yukleyin")
            print("         ve 'pkg install termux-api' calistirin.")
        else:
            print(f"  Tum zorunlu API komutlari mevcut ({len(cls.REQUIRED_API_COMMANDS)})")

        # Check optional
        available_optional = []
        for cmd in cls.OPTIONAL_API_COMMANDS:
            code, _ = cls._run_cmd(f"command -v {cmd}")
            if code == 0:
                available_optional.append(cmd)
        if available_optional:
            print(f"  Opsiyonel API komutlari: {', '.join(available_optional)}")

    @classmethod
    def _check_permissions(cls) -> None:
        """Test actual API functionality to verify permissions."""
        print("  Kamera izni kontrol ediliyor...")
        code, out = cls._run_cmd("termux-camera-info", timeout=10)
        if code == 0 and "id" in out.lower():
            print("  [OK] Kamera erisimi var")
        else:
            print("  [UYARI] Kamera erisimi yok!")
            print("  Android Ayarlar > Uygulamalar > Termux:API > Izinler > Kamera")

        print("  Pil durumu kontrol ediliyor...")
        code, out = cls._run_cmd("termux-battery-status", timeout=10)
        if code == 0 and "percentage" in out:
            print("  [OK] Pil durumu okunabiliyor")
            try:
                data = json.loads(out)
                print(f"       Pil: %{data.get('percentage', '?')}, "
                      f"Sicaklik: {data.get('temperature', '?')}C")
            except json.JSONDecodeError:
                pass
        else:
            print("  [UYARI] Pil durumu okunamiyor")

    @classmethod
    def _phantom_process_guide(cls) -> None:
        print("""
  ================================================
  ONEMLI: Phantom Process Killer (Android 12+)
  ================================================
  Android 12+ arka plan islemlerini oldurur.
  Bu ProSecCam'in kapanmasina neden olabilir.

  COZUM (ADB ile, bir kez yapilir):
    adb shell "/system/bin/device_config set_sync_disabled_for_tests persistent"
    adb shell "/system/bin/device_config put activity_manager max_phantom_processes 2147483647"
    adb shell settings put global settings_enable_monitor_phantom_procs false

  COZUM (Android 14+, ADB gerektirmez):
    Ayarlar > Gelistirici Secenekleri > Alt islem kisitlamalarini devre disi birak

  Gelistirici Secenekleri acmak icin:
    Ayarlar > Telefon Hakkinda > Yapi Numarasi'na 7 kez dokunun
        """)

    @classmethod
    def _battery_optimization_guide(cls) -> None:
        print("""
  ================================================
  ONEMLI: Pil Optimizasyonunu Kapatin
  ================================================
  Termux'un arka planda calismasi icin:

  1. Ayarlar > Uygulamalar > Termux > Pil > "Kisitlama" (Optimize etme)
  2. Ayarlar > Uygulamalar > Termux:API > Pil > "Kisitlama" (Optimize etme)
  3. Termux bildirim cubugundaki "ACQUIRE WAKELOCK" tusuna basin

  Izinler (Termux:API uygulamasi icin):
  - Kamera
  - Mikrofon
  - Depolama
  - SMS (istege bagli)
  - Telefon (istege bagli)
        """)

    @classmethod
    def _verify_all(cls) -> bool:
        """Verify all dependencies are importable."""
        all_ok = True

        checks = [
            ("Python", lambda: sys.version),
            ("numpy", lambda: __import__("numpy").__version__),
            ("cv2 (OpenCV)", lambda: __import__("cv2").__version__),
            ("ffmpeg", lambda: cls._run_cmd("ffmpeg -version")[1].split("\n")[0]
             if cls._run_cmd("ffmpeg -version")[0] == 0 else None),
            ("termux-api", lambda: "OK"
             if cls._run_cmd("command -v termux-camera-photo")[0] == 0
             else None),
        ]

        for name, check_fn in checks:
            try:
                ver = check_fn()
                if ver:
                    print(f"  [OK] {name}: {ver}")
                else:
                    print(f"  [EKSIK] {name}")
                    all_ok = False
            except Exception as e:
                print(f"  [EKSIK] {name}: {e}")
                all_ok = False

        return all_ok

    @classmethod
    def quick_check(cls) -> List[str]:
        """Quick dependency check without installing. Returns list of issues."""
        issues = []

        try:
            import numpy  # noqa: F401
        except ImportError:
            issues.append("numpy yuklu degil (pkg install python-numpy)")

        try:
            import cv2  # noqa: F401
        except ImportError:
            issues.append("opencv yuklu degil (pkg install opencv)")

        code, _ = cls._run_cmd("command -v ffmpeg")
        if code != 0:
            issues.append("ffmpeg yuklu degil (pkg install ffmpeg)")

        code, _ = cls._run_cmd("command -v termux-camera-photo")
        if code != 0:
            issues.append("termux-api yuklu degil (pkg install termux-api)")

        return issues


# ==========================================================================
# Lazy imports - only after setup verification
# ==========================================================================

def _import_cv2():
    try:
        import cv2
        return cv2
    except ImportError:
        print("[HATA] OpenCV yuklu degil!")
        print("Kurulum icin: python ProSecCam.py --setup")
        sys.exit(1)

def _import_numpy():
    try:
        import numpy
        return numpy
    except ImportError:
        print("[HATA] numpy yuklu degil!")
        print("Kurulum icin: python ProSecCam.py --setup")
        sys.exit(1)


# ==========================================================================
# EXCEPTIONS
# ==========================================================================

class ProSecCamError(Exception):
    pass

class CaptureError(ProSecCamError):
    pass

class MotionDetectionError(ProSecCamError):
    pass

class RecordingError(ProSecCamError):
    pass

# ==========================================================================
# DATA CLASSES
# ==========================================================================

@dataclass
class MotionResult:
    detected: bool
    score: float
    contour_count: int
    largest_contour_area: int
    bounding_boxes: List[Tuple[int, int, int, int]]
    timestamp: float = field(default_factory=time.time)

@dataclass
class BatteryStatus:
    percentage: int = 100
    plugged: str = "UNPLUGGED"
    temperature: float = 25.0
    status: str = "UNKNOWN"
    health: str = "GOOD"
    current: int = 0  # microamperes, negative = discharging

# ==========================================================================
# DEFAULT CONFIGURATION
# ==========================================================================

DEFAULT_CONFIG: Dict = {
    "camera_id": 0,
    "capture_interval": 2.0,
    "motion": {
        "threshold": 0.02,
        "min_contour_area": 500,
        "blur_kernel_size": 21,
        "confirmation_frames": 3,
        "confirmation_required": 2,
        "roi": None,  # [x, y, w, h] or null
    },
    "recording": {
        "min_duration_seconds": 60,
        "max_duration_seconds": 300,
        "photo_interval": 0.3,
        "include_audio": True,
        "audio_encoder": "aac",
        "audio_bitrate": 128,       # kbps
        "audio_samplerate": 44100,   # Hz
        "audio_channels": 1,         # mono
    },
    "cooldown_seconds": 15,
    "battery": {
        "low_threshold": 25,
        "critical_threshold": 10,
        "recovery_threshold": 30,
        "check_interval_seconds": 60,
        "low_capture_interval": 10.0,
        "max_temperature": 42.0,
    },
    "storage": {
        "base_path": "/data/data/com.termux/files/home/proseccam",
        "max_size_mb": 500,
        "max_age_days": 7,
        "cleanup_interval_seconds": 300,
    },
    "notifications": {
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        "sms_number": None,
        "call_number": None,
        "send_photo": True,
        "vibrate_on_motion": True,
        "vibrate_duration_ms": 500,
        "tts_enabled": False,
        "tts_message": "Hareket algilandi",
        "notification_sound": True,
        "notification_priority": "high",
        "notification_ongoing_id": "proseccam_status",
    },
    "night_mode": {
        "enabled": False,
        "torch_on_capture": True,
    },
    "logging": {
        "level": "INFO",
        "file": "proseccam.log",
        "max_bytes": 5242880,
        "backup_count": 3,
    },
}

# ==========================================================================
# CONFIG
# ==========================================================================

class Config:
    """Loads configuration from JSON, overlays CLI args, validates."""

    def __init__(self, config_path: Optional[str] = None,
                 cli_args: Optional[argparse.Namespace] = None):
        self._data: Dict = json.loads(json.dumps(DEFAULT_CONFIG))
        if config_path and os.path.isfile(config_path):
            self._load_file(config_path)
        if cli_args:
            self._merge_cli_args(cli_args)
        self._validate()

    def _load_file(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        self._deep_merge(self._data, user_cfg)

    @staticmethod
    def _deep_merge(base: Dict, override: Dict) -> None:
        for key, val in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(val, dict):
                Config._deep_merge(base[key], val)
            else:
                base[key] = val

    def _merge_cli_args(self, args: argparse.Namespace) -> None:
        mapping = {
            "camera": ("camera_id",),
            "threshold": ("motion", "threshold"),
            "no_audio": ("recording", "include_audio"),
            "night_mode": ("night_mode", "enabled"),
            "telegram_token": ("notifications", "telegram_bot_token"),
            "telegram_chat": ("notifications", "telegram_chat_id"),
            "sms": ("notifications", "sms_number"),
            "log_level": ("logging", "level"),
        }
        for arg_name, cfg_path in mapping.items():
            val = getattr(args, arg_name, None)
            if val is None:
                continue
            if arg_name == "no_audio":
                val = not val
            target = self._data
            for part in cfg_path[:-1]:
                target = target[part]
            target[cfg_path[-1]] = val

    def _validate(self) -> None:
        m = self._data["motion"]
        if m["blur_kernel_size"] % 2 == 0:
            m["blur_kernel_size"] += 1
        if m["threshold"] <= 0:
            raise ValueError("motion.threshold must be > 0")
        if self._data["recording"]["min_duration_seconds"] < 1:
            raise ValueError("recording.min_duration_seconds must be >= 1")
        if self._data["cooldown_seconds"] < 0:
            raise ValueError("cooldown_seconds must be >= 0")

    def get(self, dotted_key: str, default=None):
        parts = dotted_key.split(".")
        obj = self._data
        for p in parts:
            if isinstance(obj, dict) and p in obj:
                obj = obj[p]
            else:
                return default
        return obj

    def __getitem__(self, key: str):
        return self._data[key]

    @staticmethod
    def save_default(path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)

# ==========================================================================
# TORCH CONTROLLER
# ==========================================================================

class TorchController:
    """Manages flashlight state with thread-safe tracking."""

    def __init__(self):
        self._is_on = False
        self._lock = threading.Lock()
        self._logger = logging.getLogger("torch")

    def on(self) -> None:
        with self._lock:
            if self._is_on:
                return
            self._run("on")
            self._is_on = True
            self._logger.debug("Torch ON")

    def off(self) -> None:
        with self._lock:
            if not self._is_on:
                return
            self._run("off")
            self._is_on = False
            self._logger.debug("Torch OFF")

    def ensure_off(self) -> None:
        with self._lock:
            self._run("off")
            self._is_on = False

    @staticmethod
    def _run(state: str) -> None:
        try:
            subprocess.run(
                ["termux-torch", state],
                timeout=5, capture_output=True,
            )
        except Exception:
            pass

# ==========================================================================
# RESOURCE GUARD
# ==========================================================================

class ResourceGuard:
    """Context manager for system resources cleanup."""

    def __init__(self, torch: TorchController):
        self._wake_locked = False
        self._torch = torch
        self._cleanup_callbacks: List[Callable] = []
        self._cleaned = False
        self._logger = logging.getLogger("guard")

    def acquire_wake_lock(self) -> None:
        if not self._wake_locked:
            try:
                subprocess.run(
                    ["termux-wake-lock"],
                    timeout=5, capture_output=True,
                )
                self._wake_locked = True
                self._logger.info("Wake lock acquired")
            except Exception as e:
                self._logger.warning("Wake lock failed: %s", e)

    def release_wake_lock(self) -> None:
        if self._wake_locked:
            try:
                subprocess.run(
                    ["termux-wake-unlock"],
                    timeout=5, capture_output=True,
                )
                self._wake_locked = False
                self._logger.info("Wake lock released")
            except Exception:
                pass

    def register_cleanup(self, callback: Callable) -> None:
        self._cleanup_callbacks.append(callback)

    def cleanup_all(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._logger.info("Cleaning up all resources...")
        self._torch.ensure_off()
        self.release_wake_lock()
        try:
            subprocess.run(
                ["termux-microphone-record", "-q"],
                timeout=5, capture_output=True,
            )
        except Exception:
            pass
        for cb in self._cleanup_callbacks:
            try:
                cb()
            except Exception as e:
                self._logger.warning("Cleanup callback error: %s", e)
        # Remove ongoing notification
        try:
            subprocess.run(
                ["termux-notification-remove", "proseccam_status"],
                timeout=5, capture_output=True,
            )
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleanup_all()

# ==========================================================================
# BATTERY MONITOR
# ==========================================================================

class BatteryMonitor:
    """Periodically checks battery via termux-battery-status.
    JSON response fields: health, percentage, plugged, status, temperature, current
    """

    def __init__(self, config: Config):
        self._check_interval = config.get("battery.check_interval_seconds", 60)
        self._low_threshold = config.get("battery.low_threshold", 25)
        self._critical_threshold = config.get("battery.critical_threshold", 10)
        self._recovery_threshold = config.get("battery.recovery_threshold", 30)
        self._max_temp = config.get("battery.max_temperature", 42.0)
        self._last_check: float = 0
        self._last_status = BatteryStatus()
        self._lock = threading.Lock()
        self._logger = logging.getLogger("battery")

    def check(self) -> BatteryStatus:
        now = time.time()
        if now - self._last_check < self._check_interval:
            with self._lock:
                return self._last_status
        return self._run_check()

    def force_check(self) -> BatteryStatus:
        return self._run_check()

    def _run_check(self) -> BatteryStatus:
        try:
            result = subprocess.run(
                ["termux-battery-status"],
                timeout=10, capture_output=True, text=True,
            )
            data = json.loads(result.stdout)
            status = BatteryStatus(
                percentage=int(data.get("percentage", 100)),
                plugged=str(data.get("plugged", "UNPLUGGED")),
                temperature=float(data.get("temperature", 25.0)),
                status=str(data.get("status", "UNKNOWN")),
                health=str(data.get("health", "GOOD")),
                current=int(data.get("current", 0)),
            )
            with self._lock:
                self._last_status = status
                self._last_check = time.time()
            return status
        except Exception as e:
            self._logger.warning("Battery check failed: %s", e)
            with self._lock:
                return self._last_status

    @property
    def percentage(self) -> int:
        with self._lock:
            return self._last_status.percentage

    @property
    def is_charging(self) -> bool:
        with self._lock:
            return self._last_status.plugged != "UNPLUGGED"

    @property
    def is_low(self) -> bool:
        with self._lock:
            return (self._last_status.percentage <= self._low_threshold
                    and self._last_status.plugged == "UNPLUGGED")

    @property
    def is_critical(self) -> bool:
        with self._lock:
            return (self._last_status.percentage <= self._critical_threshold
                    and self._last_status.plugged == "UNPLUGGED")

    @property
    def is_recovered(self) -> bool:
        with self._lock:
            return self._last_status.percentage >= self._recovery_threshold

    @property
    def is_overheating(self) -> bool:
        with self._lock:
            return self._last_status.temperature > self._max_temp

    @property
    def health(self) -> str:
        with self._lock:
            return self._last_status.health

# ==========================================================================
# STORAGE MANAGER
# ==========================================================================

class StorageManager:
    """Manages recording storage, cleanup, and directory structure."""

    def __init__(self, config: Config):
        self._base_path = config.get(
            "storage.base_path",
            "/data/data/com.termux/files/home/proseccam",
        )
        self._max_size_bytes = config.get("storage.max_size_mb", 500) * 1024 * 1024
        self._max_age_seconds = config.get("storage.max_age_days", 7) * 86400
        self._cleanup_interval = config.get("storage.cleanup_interval_seconds", 300)
        self._last_cleanup: float = 0
        self._logger = logging.getLogger("storage")

    @property
    def base_path(self) -> str:
        return self._base_path

    def initialize(self) -> None:
        for subdir in ["events", "temp", "logs"]:
            os.makedirs(os.path.join(self._base_path, subdir), exist_ok=True)
        self._logger.info("Storage initialized: %s", self._base_path)

    def get_event_dir(self, timestamp: Optional[str] = None) -> str:
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self._base_path, "events", timestamp)
        os.makedirs(path, exist_ok=True)
        return path

    def get_temp_dir(self) -> str:
        path = os.path.join(self._base_path, "temp")
        os.makedirs(path, exist_ok=True)
        return path

    def clear_temp(self) -> None:
        temp = self.get_temp_dir()
        for f in os.listdir(temp):
            try:
                fp = os.path.join(temp, f)
                if os.path.isfile(fp):
                    os.remove(fp)
            except OSError:
                pass

    def cleanup_if_needed(self) -> None:
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        self._cleanup()

    def _cleanup(self) -> None:
        events_dir = os.path.join(self._base_path, "events")
        if not os.path.isdir(events_dir):
            return
        dirs = sorted(
            [d for d in os.listdir(events_dir)
             if os.path.isdir(os.path.join(events_dir, d))]
        )
        now = time.time()

        # Age-based cleanup
        for d in dirs[:]:
            dpath = os.path.join(events_dir, d)
            try:
                mtime = os.path.getmtime(dpath)
                if now - mtime > self._max_age_seconds:
                    shutil.rmtree(dpath, ignore_errors=True)
                    dirs.remove(d)
                    self._logger.info("Deleted old event: %s", d)
            except OSError:
                pass

        # Size-based cleanup
        total = self._get_dir_size(events_dir)
        for d in dirs:
            if total <= self._max_size_bytes:
                break
            dpath = os.path.join(events_dir, d)
            dsize = self._get_dir_size(dpath)
            shutil.rmtree(dpath, ignore_errors=True)
            total -= dsize
            self._logger.info("Deleted for space: %s (freed %d KB)",
                              d, dsize // 1024)
        self.clear_temp()

    def get_usage(self) -> Dict:
        events_dir = os.path.join(self._base_path, "events")
        if not os.path.isdir(events_dir):
            return {"total_bytes": 0, "event_count": 0}
        dirs = sorted(
            [d for d in os.listdir(events_dir)
             if os.path.isdir(os.path.join(events_dir, d))]
        )
        return {
            "total_bytes": self._get_dir_size(events_dir),
            "event_count": len(dirs),
            "oldest": dirs[0] if dirs else None,
            "newest": dirs[-1] if dirs else None,
        }

    @staticmethod
    def _get_dir_size(path: str) -> int:
        total = 0
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total

# ==========================================================================
# CAMERA CAPTURE
# ==========================================================================

class CameraCapture:
    """Wraps termux-camera-photo with timeout and retry.
    Usage: termux-camera-photo -c <camera_id> <output_file>
    """

    def __init__(self, config: Config):
        self._camera_id = config.get("camera_id", 0)
        self._timeout = 15.0
        self._logger = logging.getLogger("camera")

    def capture(self, output_path: str) -> bool:
        """Capture a single photo. Returns True on success."""
        try:
            subprocess.run(
                ["termux-camera-photo", "-c",
                 str(self._camera_id), output_path],
                timeout=self._timeout,
                capture_output=True, text=True,
            )
            # Wait for file to be written
            for _ in range(30):
                if os.path.isfile(output_path) and os.path.getsize(output_path) > 100:
                    return True
                time.sleep(0.2)
            self._logger.warning("Photo not found: %s", output_path)
            return False
        except subprocess.TimeoutExpired:
            self._logger.error("Camera timeout")
            raise CaptureError("Camera capture timed out")
        except Exception as e:
            self._logger.error("Camera failed: %s", e)
            raise CaptureError(str(e))

    def capture_burst(self, output_dir: str, count: int, interval: float,
                      stop_event: threading.Event) -> List[str]:
        """Capture burst photos. Returns list of file paths."""
        files: List[str] = []
        for i in range(count):
            if stop_event.is_set():
                break
            fname = os.path.join(output_dir, f"frame_{i:04d}.jpg")
            try:
                if self.capture(fname):
                    files.append(fname)
            except CaptureError:
                self._logger.warning("Burst frame %d failed", i)
            if not stop_event.is_set() and i < count - 1:
                stop_event.wait(timeout=interval)
        return files

    def get_camera_info(self) -> Optional[str]:
        """Get camera info via termux-camera-info."""
        try:
            result = subprocess.run(
                ["termux-camera-info"],
                timeout=10, capture_output=True, text=True,
            )
            return result.stdout if result.returncode == 0 else None
        except Exception:
            return None

# ==========================================================================
# MOTION DETECTOR
# ==========================================================================

class MotionDetector:
    """OpenCV-based motion detection with adaptive thresholding.

    Algorithm:
    1. Resize to 640x480, convert to grayscale
    2. GaussianBlur to reduce sensor noise
    3. absdiff against background accumulator
    4. Binary threshold + dilate
    5. Contour analysis with min area filter
    6. Background updated only when no motion (prevents absorption)
    7. Confirmation: 2/3 rapid frames must detect motion
    """

    PROCESS_WIDTH = 640
    PROCESS_HEIGHT = 480

    def __init__(self, config: Config):
        self._threshold = config.get("motion.threshold", 0.02)
        self._min_contour_area = config.get("motion.min_contour_area", 500)
        self._blur_kernel = config.get("motion.blur_kernel_size", 21)
        if self._blur_kernel % 2 == 0:
            self._blur_kernel += 1
        self._conf_frames = config.get("motion.confirmation_frames", 3)
        self._conf_required = config.get("motion.confirmation_required", 2)
        roi = config.get("motion.roi")
        self._roi: Optional[Tuple[int, int, int, int]] = tuple(roi) if roi else None
        self._bg: Optional["numpy.ndarray"] = None
        self._alpha = 0.05  # background learning rate
        self._calibrated = False
        self._baseline_noise = 0.0
        self._logger = logging.getLogger("motion")

    def calibrate(self, frames: list) -> None:
        """Calibrate with startup frames to determine baseline noise."""
        cv2 = _import_cv2()
        np = _import_numpy()

        if len(frames) < 2:
            self._logger.warning("Not enough calibration frames, using defaults")
            self._calibrated = True
            return

        processed = [self._preprocess(f) for f in frames]
        self._bg = np.mean(processed, axis=0).astype(np.float32)

        diffs = []
        for i in range(len(processed) - 1):
            diff = cv2.absdiff(processed[i], processed[i + 1])
            score = float(np.sum(diff > 25)) / float(diff.size)
            diffs.append(score)

        self._baseline_noise = float(np.mean(diffs)) if diffs else 0.0
        effective = max(self._threshold, self._baseline_noise * 2.5)
        self._logger.info(
            "Calibration: baseline_noise=%.5f, threshold=%.5f (was %.5f)",
            self._baseline_noise, effective, self._threshold)
        self._threshold = effective
        self._calibrated = True

    def detect(self, frame) -> MotionResult:
        """Analyze single frame against background model."""
        cv2 = _import_cv2()
        np = _import_numpy()

        gray = self._preprocess(frame)
        if self._bg is None:
            self._bg = gray.astype(np.float32)
            return MotionResult(False, 0.0, 0, 0, [])

        bg_uint8 = self._bg.astype(np.uint8)
        diff = cv2.absdiff(gray, bg_uint8)
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=2)

        # Apply ROI mask
        if self._roi:
            mask = np.zeros_like(thresh)
            x, y, w, h = self._roi
            mask[y:y+h, x:x+w] = 255
            thresh = cv2.bitwise_and(thresh, mask)

        contours, _ = cv2.findContours(
            thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        total_area = 0
        largest = 0
        bboxes: List[Tuple[int, int, int, int]] = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < self._min_contour_area:
                continue
            total_area += area
            if area > largest:
                largest = int(area)
            bboxes.append(tuple(cv2.boundingRect(cnt)))

        total_pixels = gray.shape[0] * gray.shape[1]
        score = total_area / total_pixels if total_pixels > 0 else 0.0
        detected = score > self._threshold

        # Update background only when no motion
        if not detected:
            cv2.accumulateWeighted(gray, self._bg, self._alpha)

        return MotionResult(
            detected=detected,
            score=round(score, 6),
            contour_count=len(bboxes),
            largest_contour_area=largest,
            bounding_boxes=bboxes,
        )

    def confirm_motion(self, capture_func: Callable) -> Tuple[bool, Optional[str]]:
        """Capture confirmation frames and verify motion.
        Returns (confirmed, trigger_frame_path_or_none).
        """
        positives = 0
        for i in range(self._conf_frames):
            frame = capture_func()
            if frame is None:
                continue
            result = self.detect(frame)
            if result.detected:
                positives += 1
            if i < self._conf_frames - 1:
                time.sleep(0.5)
        confirmed = positives >= self._conf_required
        self._logger.info("Confirmation: %d/%d -> %s",
                          positives, self._conf_frames,
                          "CONFIRMED" if confirmed else "rejected")
        return confirmed, None

    def reset(self) -> None:
        self._bg = None
        self._calibrated = False

    def _preprocess(self, frame):
        cv2 = _import_cv2()
        resized = cv2.resize(frame, (self.PROCESS_WIDTH, self.PROCESS_HEIGHT))
        if len(resized.shape) == 3:
            gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        else:
            gray = resized
        return cv2.GaussianBlur(gray, (self._blur_kernel, self._blur_kernel), 0)

# ==========================================================================
# RECORDING MANAGER
# ==========================================================================

class RecordingManager:
    """Coordinates photo burst, audio recording, and ffmpeg encoding.

    Audio: termux-microphone-record -f <file> -e <encoder> -b <bitrate>
                                    -r <samplerate> -c <channels>
    Stop:  termux-microphone-record -q
    Video: ffmpeg -framerate <fps> -i frame_%04d.jpg [-i audio.aac]
                  -c:v libx264 -tune stillimage -pix_fmt yuv420p -shortest out.mp4
    """

    def __init__(self, config: Config, camera: CameraCapture,
                 storage: StorageManager, torch: TorchController):
        self._min_duration = config.get("recording.min_duration_seconds", 60)
        self._max_duration = config.get("recording.max_duration_seconds", 300)
        self._photo_interval = config.get("recording.photo_interval", 0.3)
        self._include_audio = config.get("recording.include_audio", True)
        self._audio_encoder = config.get("recording.audio_encoder", "aac")
        self._audio_bitrate = config.get("recording.audio_bitrate", 128)
        self._audio_samplerate = config.get("recording.audio_samplerate", 44100)
        self._audio_channels = config.get("recording.audio_channels", 1)
        self._camera = camera
        self._storage = storage
        self._torch = torch
        self._night_mode = config.get("night_mode.enabled", False)
        self._torch_on_capture = config.get("night_mode.torch_on_capture", True)
        self._recording = False
        self._stop_event = threading.Event()
        self._audio_process: Optional[subprocess.Popen] = None
        self._logger = logging.getLogger("recording")

    def start(self, trigger_frame_path: Optional[str] = None) -> Optional[str]:
        """Begin recording session. Returns event directory path."""
        self._recording = True
        self._stop_event.clear()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        event_dir = self._storage.get_event_dir(timestamp)
        self._logger.info("Recording started: %s", event_dir)

        # Copy trigger frame
        if trigger_frame_path and os.path.isfile(trigger_frame_path):
            shutil.copy2(trigger_frame_path,
                         os.path.join(event_dir, "trigger.jpg"))

        audio_path = os.path.join(event_dir, "audio.aac")
        if self._include_audio:
            self._start_audio(audio_path)

        if self._night_mode and self._torch_on_capture:
            self._torch.on()

        # Photo burst
        start_time = time.time()
        max_frames = int(self._max_duration / max(self._photo_interval, 0.1))
        photo_files: List[str] = []
        try:
            for i in range(max_frames):
                if self._stop_event.is_set():
                    if time.time() - start_time >= self._min_duration:
                        break
                fname = os.path.join(event_dir, f"frame_{i:04d}.jpg")
                try:
                    if self._camera.capture(fname):
                        photo_files.append(fname)
                except CaptureError:
                    self._logger.warning("Frame %d failed", i)

                elapsed = time.time() - start_time
                if elapsed >= self._max_duration:
                    break
                if elapsed >= self._min_duration and self._stop_event.is_set():
                    break

                if not self._stop_event.is_set():
                    self._stop_event.wait(timeout=self._photo_interval)
        finally:
            if self._night_mode and self._torch_on_capture:
                self._torch.off()
            if self._include_audio:
                self._stop_audio()

        duration = time.time() - start_time
        self._logger.info("Captured %d frames in %.1fs", len(photo_files), duration)

        # Encode video
        video_path = None
        if len(photo_files) >= 2:
            video_path = self._encode_video(
                event_dir, photo_files,
                audio_path if self._include_audio else None,
            )
            if video_path:
                for pf in photo_files:
                    try:
                        os.remove(pf)
                    except OSError:
                        pass

        # Write metadata
        bat_pct = -1
        try:
            result = subprocess.run(
                ["termux-battery-status"],
                timeout=5, capture_output=True, text=True,
            )
            bat_pct = json.loads(result.stdout).get("percentage", -1)
        except Exception:
            pass

        metadata = {
            "timestamp": timestamp,
            "duration_seconds": round(duration, 1),
            "frame_count": len(photo_files),
            "has_video": video_path is not None,
            "has_audio": self._include_audio,
            "battery_percent": bat_pct,
            "camera_id": self._camera._camera_id,
            "night_mode": self._night_mode,
        }
        try:
            with open(os.path.join(event_dir, "metadata.json"), "w",
                       encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
        except OSError as e:
            self._logger.warning("Metadata write failed: %s", e)

        self._recording = False
        return event_dir

    def _start_audio(self, output_path: str) -> None:
        """Start audio: termux-microphone-record -f FILE -e ENCODER -b BITRATE
        -r SAMPLERATE -c CHANNELS"""
        try:
            cmd = [
                "termux-microphone-record",
                "-f", output_path,
                "-e", self._audio_encoder,
                "-b", str(self._audio_bitrate),
                "-r", str(self._audio_samplerate),
                "-c", str(self._audio_channels),
            ]
            self._audio_process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._logger.info("Audio started: %s", " ".join(cmd))
        except Exception as e:
            self._logger.warning("Audio start failed: %s", e)
            self._audio_process = None

    def _stop_audio(self) -> None:
        """Stop audio: termux-microphone-record -q"""
        try:
            subprocess.run(
                ["termux-microphone-record", "-q"],
                timeout=10, capture_output=True,
            )
            self._logger.info("Audio stopped")
        except Exception as e:
            self._logger.warning("Audio stop failed: %s", e)
        if self._audio_process:
            try:
                self._audio_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._audio_process.kill()
                self._audio_process.wait()
            self._audio_process = None

    def stop(self) -> None:
        self._stop_event.set()

    def _encode_video(self, event_dir: str, photo_files: List[str],
                      audio_path: Optional[str]) -> Optional[str]:
        output = os.path.join(event_dir, "recording.mp4")
        if len(photo_files) < 2:
            return None
        try:
            t_first = os.path.getmtime(photo_files[0])
            t_last = os.path.getmtime(photo_files[-1])
            elapsed = t_last - t_first
            fps = round(len(photo_files) / elapsed, 1) if elapsed > 0 else 3.0
        except OSError:
            fps = 3.0
        fps = max(1.0, min(fps, 30.0))

        input_pattern = os.path.join(event_dir, "frame_%04d.jpg")
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", input_pattern,
        ]
        if audio_path and os.path.isfile(audio_path):
            cmd += ["-i", audio_path, "-c:a", "aac", "-b:a", "128k"]
        cmd += [
            "-c:v", "libx264", "-tune", "stillimage",
            "-pix_fmt", "yuv420p", "-shortest",
            output,
        ]
        try:
            result = subprocess.run(
                cmd, timeout=180, capture_output=True, text=True,
            )
            if result.returncode == 0 and os.path.isfile(output):
                size_mb = os.path.getsize(output) / (1024 * 1024)
                self._logger.info("Video: %s (%.1f fps, %.1f MB)",
                                  output, fps, size_mb)
                return output
            self._logger.error("ffmpeg failed: %s", result.stderr[:500])
            return None
        except subprocess.TimeoutExpired:
            self._logger.error("ffmpeg timeout")
            return None
        except FileNotFoundError:
            self._logger.error("ffmpeg not found! pkg install ffmpeg")
            return None

    @property
    def is_recording(self) -> bool:
        return self._recording

# ==========================================================================
# NOTIFICATION MANAGER
# ==========================================================================

class NotificationManager:
    """Sends alerts via Telegram, SMS, termux-notification, vibrate, TTS.

    termux-notification flags:
      --title, --content, --id, --ongoing, --sound, --vibrate pattern,
      --priority (high/low/max/min/default), --image-path, --alert-once,
      --action (tap command), --button1/2/3 text, --button1/2/3-action

    termux-vibrate: -d <ms> -f (force)
    termux-tts-speak: [-l lang] [-r rate] text
    termux-sms-send: -n <number> <text>
    """

    def __init__(self, config: Config):
        self._telegram_token = config.get("notifications.telegram_bot_token")
        self._telegram_chat_id = config.get("notifications.telegram_chat_id")
        self._sms_number = config.get("notifications.sms_number")
        self._call_number = config.get("notifications.call_number")
        self._send_photo = config.get("notifications.send_photo", True)
        self._vibrate = config.get("notifications.vibrate_on_motion", True)
        self._vibrate_ms = config.get("notifications.vibrate_duration_ms", 500)
        self._tts_enabled = config.get("notifications.tts_enabled", False)
        self._tts_message = config.get("notifications.tts_message",
                                       "Hareket algilandi")
        self._notif_sound = config.get("notifications.notification_sound", True)
        self._notif_priority = config.get("notifications.notification_priority",
                                          "high")
        self._ongoing_id = config.get("notifications.notification_ongoing_id",
                                      "proseccam_status")
        self._queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False
        self._logger = logging.getLogger("notify")

    def start(self) -> None:
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker, daemon=True, name="notifier")
        self._worker_thread.start()

    def _worker(self) -> None:
        while self._running or not self._queue.empty():
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                func, args, kwargs = task
                func(*args, **kwargs)
            except Exception as e:
                self._logger.warning("Notification failed: %s", e)
            self._queue.task_done()

    def notify_motion(self, event_dir: str,
                      photo_path: Optional[str] = None,
                      motion_score: float = 0.0) -> None:
        """Queue all motion alerts."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"Hareket Algilandi!\nZaman: {ts}\nSkor: {motion_score:.4f}"

        # Termux notification with sound and image
        notif_args = {
            "title": "ProSecCam - Hareket!",
            "content": msg,
            "priority": self._notif_priority,
            "sound": self._notif_sound,
            "image_path": photo_path,
        }
        self._queue.put((self._show_notification, (), notif_args))

        # Vibrate
        if self._vibrate:
            self._queue.put((self._do_vibrate, (self._vibrate_ms,), {}))

        # TTS
        if self._tts_enabled:
            self._queue.put((self._do_tts, (self._tts_message,), {}))

        # Telegram
        if self._telegram_token and self._telegram_chat_id:
            self._queue.put((self._send_telegram_message, (msg,), {}))
            if self._send_photo and photo_path and os.path.isfile(photo_path):
                self._queue.put((self._send_telegram_photo,
                                 (photo_path, f"Hareket: {ts}"), {}))

        # SMS
        if self._sms_number:
            self._queue.put((self._send_sms, (msg,), {}))

        # Phone call
        if self._call_number:
            self._queue.put((self._make_call,), {})

    def notify_status(self, message: str) -> None:
        """Send status update (startup, shutdown, battery warnings)."""
        self._queue.put((self._show_notification, (), {
            "title": "ProSecCam",
            "content": message,
            "notification_id": self._ongoing_id,
            "ongoing": True,
        }))
        if self._telegram_token and self._telegram_chat_id:
            self._queue.put((self._send_telegram_message, (message,), {}))

    def update_ongoing(self, state: str, battery: int,
                       events: int = 0) -> None:
        """Update persistent status notification."""
        content = (f"Durum: {state} | Pil: %{battery} | "
                   f"Kayit: {events}")
        self._queue.put((self._show_notification, (), {
            "title": "ProSecCam Aktif",
            "content": content,
            "notification_id": self._ongoing_id,
            "ongoing": True,
            "alert_once": True,
        }))

    def _show_notification(self, title: str = "", content: str = "",
                           notification_id: Optional[str] = None,
                           ongoing: bool = False,
                           sound: bool = False,
                           priority: str = "default",
                           image_path: Optional[str] = None,
                           alert_once: bool = False) -> None:
        """termux-notification with full flag support."""
        cmd = ["termux-notification"]
        if title:
            cmd += ["--title", title]
        if content:
            cmd += ["--content", content[:256]]
        if notification_id:
            cmd += ["--id", notification_id]
        if ongoing:
            cmd += ["--ongoing"]
        if sound:
            cmd += ["--sound"]
        if alert_once:
            cmd += ["--alert-once"]
        if priority and priority != "default":
            cmd += ["--priority", priority]
        if image_path and os.path.isfile(image_path):
            cmd += ["--image-path", image_path]
        try:
            subprocess.run(cmd, timeout=10, capture_output=True)
        except Exception:
            pass

    @staticmethod
    def _do_vibrate(duration_ms: int = 500) -> None:
        """termux-vibrate -d <ms> -f"""
        try:
            subprocess.run(
                ["termux-vibrate", "-d", str(duration_ms), "-f"],
                timeout=5, capture_output=True,
            )
        except Exception:
            pass

    @staticmethod
    def _do_tts(text: str) -> None:
        """termux-tts-speak text"""
        try:
            subprocess.run(
                ["termux-tts-speak", text],
                timeout=15, capture_output=True,
            )
        except Exception:
            pass

    def _send_telegram_message(self, text: str) -> bool:
        try:
            url = (f"https://api.telegram.org/bot{self._telegram_token}"
                   f"/sendMessage")
            data = json.dumps({
                "chat_id": self._telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
            })
            subprocess.run(
                ["curl", "-s", "-X", "POST", url,
                 "-H", "Content-Type: application/json",
                 "-d", data],
                timeout=30, capture_output=True,
            )
            return True
        except Exception as e:
            self._logger.warning("Telegram msg failed: %s", e)
            return False

    def _send_telegram_photo(self, photo_path: str, caption: str) -> bool:
        try:
            url = (f"https://api.telegram.org/bot{self._telegram_token}"
                   f"/sendPhoto")
            subprocess.run(
                ["curl", "-s", "-X", "POST", url,
                 "-F", f"chat_id={self._telegram_chat_id}",
                 "-F", f"photo=@{photo_path}",
                 "-F", f"caption={caption}"],
                timeout=60, capture_output=True,
            )
            return True
        except Exception as e:
            self._logger.warning("Telegram photo failed: %s", e)
            return False

    def _send_sms(self, message: str) -> bool:
        """termux-sms-send -n <number> <message>"""
        try:
            subprocess.run(
                ["termux-sms-send", "-n", self._sms_number, message],
                timeout=15, capture_output=True,
            )
            return True
        except Exception as e:
            self._logger.warning("SMS failed: %s", e)
            return False

    def _make_call(self) -> bool:
        """termux-telephony-call <number>"""
        try:
            subprocess.run(
                ["termux-telephony-call", self._call_number],
                timeout=15, capture_output=True,
            )
            return True
        except Exception as e:
            self._logger.warning("Call failed: %s", e)
            return False

    def stop(self) -> None:
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=10)

# ==========================================================================
# STATE MACHINE
# ==========================================================================

class CamState(enum.Enum):
    INITIALIZING = "initializing"
    IDLE = "idle"
    DETECTING = "detecting"
    RECORDING = "recording"
    COOLDOWN = "cooldown"
    LOW_BATTERY = "low_battery"
    PAUSED = "paused"
    SHUTTING_DOWN = "shutting_down"

# ==========================================================================
# MAIN APPLICATION
# ==========================================================================

class ProSecCam:
    """Main application with state machine, signal handling, and full
    resource lifecycle management."""

    def __init__(self, config: Config):
        self._config = config
        self._state = CamState.INITIALIZING
        self._running = False
        self._dry_run = False

        # Components
        self._torch = TorchController()
        self._guard = ResourceGuard(self._torch)
        self._battery = BatteryMonitor(config)
        self._storage = StorageManager(config)
        self._camera = CameraCapture(config)
        self._detector = MotionDetector(config)
        self._recorder = RecordingManager(
            config, self._camera, self._storage, self._torch)
        self._notifier = NotificationManager(config)

        # Timing
        self._capture_interval = config.get("capture_interval", 2.0)
        self._low_capture_interval = config.get(
            "battery.low_capture_interval", 10.0)
        self._cooldown_seconds = config.get("cooldown_seconds", 15)
        self._cooldown_start: float = 0

        # Stats
        self._total_events = 0
        self._start_time: float = 0

        self._logger = logging.getLogger("proseccam")

    def run(self, dry_run: bool = False) -> None:
        self._dry_run = dry_run
        self._running = True
        self._start_time = time.time()

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        with self._guard:
            atexit.register(self._guard.cleanup_all)
            try:
                self._initialize()
                while self._running:
                    try:
                        self._tick()
                    except CaptureError as e:
                        self._logger.error("Capture: %s. Retry 5s.", e)
                        time.sleep(5)
                    except MotionDetectionError as e:
                        self._logger.error("Detection: %s. Reset.", e)
                        self._detector.reset()
                    except RecordingError as e:
                        self._logger.error("Recording: %s", e)
                        self._recorder.stop()
                        self._transition(CamState.COOLDOWN)
                        self._cooldown_start = time.time()
                    except Exception as e:
                        self._logger.critical("Unexpected: %s", e, exc_info=True)
                        time.sleep(10)
            finally:
                self._notifier.notify_status("ProSecCam durduruluyor...")
                time.sleep(1)  # Let notification send
                self._notifier.stop()
                self._logger.info("ProSecCam stopped. Total events: %d",
                                  self._total_events)

    def _initialize(self) -> None:
        cv2 = _import_cv2()
        self._logger.info("=" * 50)
        self._logger.info("ProSecCam v%s starting...", __version__)

        # Quick dependency check
        issues = TermuxSetup.quick_check()
        if issues:
            for issue in issues:
                self._logger.warning("Dependency: %s", issue)
            self._logger.warning("Bazi bagimliliklar eksik! --setup ile kurun.")

        self._storage.initialize()
        self._guard.acquire_wake_lock()

        # Battery check
        bat = self._battery.force_check()
        self._logger.info("Battery: %d%% (%s), Temp: %.1fC, Health: %s",
                          bat.percentage, bat.plugged,
                          bat.temperature, bat.health)

        # Camera info
        cam_info = self._camera.get_camera_info()
        if cam_info:
            self._logger.info("Camera info:\n%s", cam_info.strip())

        # Start notifications
        self._notifier.start()
        self._notifier.notify_status(
            f"ProSecCam baslatiliyor... Pil: {bat.percentage}%")

        # Calibration
        self._logger.info("Calibrating motion detector (10 frames)...")
        temp_dir = self._storage.get_temp_dir()
        frames = []
        for i in range(10):
            if not self._running:
                return
            fpath = os.path.join(temp_dir, f"cal_{i}.jpg")
            try:
                if self._camera.capture(fpath):
                    frame = cv2.imread(fpath)
                    if frame is not None:
                        frames.append(frame)
                        self._logger.debug("Calibration frame %d OK", i)
            except CaptureError:
                self._logger.warning("Calibration frame %d failed", i)
            time.sleep(2)

        self._detector.calibrate(frames)
        self._storage.clear_temp()

        # Storage usage
        usage = self._storage.get_usage()
        self._logger.info("Storage: %d events, %.1f MB",
                          usage["event_count"],
                          usage["total_bytes"] / (1024 * 1024))

        self._notifier.notify_status("ProSecCam hazir - izleme basladi.")
        self._logger.info("Initialization complete. Mode: %s",
                          "DRY-RUN" if self._dry_run else "LIVE")
        self._transition(CamState.IDLE)

    def _tick(self) -> None:
        handlers = {
            CamState.IDLE: self._tick_idle,
            CamState.DETECTING: self._tick_detecting,
            CamState.RECORDING: self._tick_recording,
            CamState.COOLDOWN: self._tick_cooldown,
            CamState.LOW_BATTERY: self._tick_low_battery,
            CamState.PAUSED: self._tick_paused,
        }
        handler = handlers.get(self._state)
        if handler:
            handler()

    def _tick_idle(self) -> None:
        bat = self._battery.check()
        if self._battery.is_overheating:
            self._logger.warning("Overheating! %.1fC", bat.temperature)
            self._notifier.notify_status(
                f"Asiri isinma: {bat.temperature}C. Yavaslatiliyor.")
            self._transition(CamState.LOW_BATTERY)
            return
        if self._battery.is_low:
            self._logger.warning("Low battery: %d%%", bat.percentage)
            self._transition(CamState.LOW_BATTERY)
            return

        self._storage.cleanup_if_needed()

        # Update ongoing notification periodically
        self._notifier.update_ongoing(
            "Izleniyor", bat.percentage, self._total_events)

        result = self._capture_and_detect()
        if result and result.detected:
            self._logger.info("Motion suspected! Score: %.4f", result.score)
            self._transition(CamState.DETECTING)
            return

        time.sleep(self._capture_interval)

    def _tick_detecting(self) -> None:
        temp_dir = self._storage.get_temp_dir()
        cv2 = _import_cv2()

        def capture_frame():
            fpath = os.path.join(temp_dir, "confirm.jpg")
            try:
                if self._camera.capture(fpath):
                    return cv2.imread(fpath)
            except CaptureError:
                pass
            return None

        confirmed, _ = self._detector.confirm_motion(capture_frame)
        if confirmed:
            self._logger.info("Motion CONFIRMED!")
            if self._dry_run:
                self._logger.info("[DRY RUN] Recording skipped")
                self._transition(CamState.COOLDOWN)
                self._cooldown_start = time.time()
            else:
                self._transition(CamState.RECORDING)
        else:
            self._logger.info("False alarm -> IDLE")
            self._transition(CamState.IDLE)

    def _tick_recording(self) -> None:
        trigger = os.path.join(self._storage.get_temp_dir(), "confirm.jpg")
        if not os.path.isfile(trigger):
            trigger = None

        event_dir = self._recorder.start(trigger_frame_path=trigger)
        self._total_events += 1

        if event_dir:
            photo = os.path.join(event_dir, "trigger.jpg")
            if not os.path.isfile(photo):
                for f in sorted(os.listdir(event_dir)):
                    if f.endswith(".jpg"):
                        photo = os.path.join(event_dir, f)
                        break
            self._notifier.notify_motion(
                event_dir,
                photo_path=photo if os.path.isfile(photo) else None,
            )

        self._transition(CamState.COOLDOWN)
        self._cooldown_start = time.time()

    def _tick_cooldown(self) -> None:
        elapsed = time.time() - self._cooldown_start
        if elapsed >= self._cooldown_seconds:
            self._battery.check()
            if self._battery.is_low or self._battery.is_overheating:
                self._transition(CamState.LOW_BATTERY)
            else:
                self._transition(CamState.IDLE)
        else:
            remaining = self._cooldown_seconds - elapsed
            time.sleep(min(remaining, 2.0))

    def _tick_low_battery(self) -> None:
        bat = self._battery.check()
        if self._battery.is_critical and not self._battery.is_charging:
            self._logger.warning("Critical: %d%%. Pausing.", bat.percentage)
            self._notifier.notify_status(
                f"Kritik pil: {bat.percentage}%. Duraklatildi.")
            self._transition(CamState.PAUSED)
            return
        if self._battery.is_recovered and not self._battery.is_overheating:
            self._logger.info("Battery recovered: %d%%", bat.percentage)
            self._transition(CamState.IDLE)
            return

        self._storage.cleanup_if_needed()
        result = self._capture_and_detect()
        if result and result.detected:
            self._logger.info("Motion in low-battery! Score: %.4f",
                              result.score)
            self._transition(CamState.DETECTING)
            return
        time.sleep(self._low_capture_interval)

    def _tick_paused(self) -> None:
        time.sleep(30)
        bat = self._battery.force_check()
        self._logger.info("Paused. Battery: %d%% (%s)", bat.percentage, bat.plugged)
        if self._battery.is_recovered or self._battery.is_charging:
            self._logger.info("Resuming from pause")
            self._notifier.notify_status(
                f"Pil yeterli: {bat.percentage}%. Devam ediliyor.")
            self._transition(CamState.IDLE)

    def _capture_and_detect(self) -> Optional[MotionResult]:
        cv2 = _import_cv2()
        temp_dir = self._storage.get_temp_dir()
        fpath = os.path.join(temp_dir, "current.jpg")
        try:
            if not self._camera.capture(fpath):
                return None
            frame = cv2.imread(fpath)
            if frame is None:
                return None
            return self._detector.detect(frame)
        except CaptureError:
            return None

    def _transition(self, new_state: CamState) -> None:
        old = self._state
        self._state = new_state
        self._logger.info("State: %s -> %s", old.value, new_state.value)

    def _handle_signal(self, signum: int, frame) -> None:
        sig_name = signal.Signals(signum).name
        self._logger.info("Signal %s received. Shutting down...", sig_name)
        self._running = False
        if self._recorder.is_recording:
            self._recorder.stop()
        self._state = CamState.SHUTTING_DOWN

# ==========================================================================
# LOGGING SETUP
# ==========================================================================

def setup_logging(config: Config) -> None:
    level_str = config.get("logging.level", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)
    log_file = config.get("logging.file", "proseccam.log")
    base_path = config.get("storage.base_path",
                           "/data/data/com.termux/files/home/proseccam")
    log_dir = os.path.join(base_path, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, log_file)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)

    fh = RotatingFileHandler(
        log_path,
        maxBytes=config.get("logging.max_bytes", 5242880),
        backupCount=config.get("logging.backup_count", 3),
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

# ==========================================================================
# CLI & MAIN
# ==========================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"ProSecCam v{__version__} - Professional Security Camera",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ornekler:
  %(prog)s --setup                     Ilk kurulum (otomatik)
  %(prog)s --init-config               Config dosyasi olustur
  %(prog)s --dry-run                   Test (kayit yapmaz)
  %(prog)s --camera 1 --night-mode     On kamera + fener
  %(prog)s --telegram-token T --telegram-chat C
  %(prog)s --check                     Bagimliliklari kontrol et
        """,
    )

    # Setup & utility
    setup_grp = parser.add_argument_group("Kurulum")
    setup_grp.add_argument(
        "--setup", action="store_true",
        help="Otomatik kurulum (paketler, izinler, bagimliliklar)")
    setup_grp.add_argument(
        "--init-config", action="store_true",
        help="Varsayilan config dosyasi olustur")
    setup_grp.add_argument(
        "--check", action="store_true",
        help="Bagimliliklari kontrol et (kurulum yapmadan)")

    # Runtime
    run_grp = parser.add_argument_group("Calistirma")
    run_grp.add_argument(
        "--config", type=str, default=None,
        help="Config JSON dosya yolu")
    run_grp.add_argument(
        "--camera", type=int, default=None,
        help="Kamera ID (0=arka, 1=on)")
    run_grp.add_argument(
        "--threshold", type=float, default=None,
        help="Hareket algilama esigi (0.0-1.0)")
    run_grp.add_argument(
        "--no-audio", action="store_true", default=None,
        help="Ses kaydini devre disi birak")
    run_grp.add_argument(
        "--night-mode", action="store_true", default=None,
        help="Gece modu (fener ile)")
    run_grp.add_argument(
        "--dry-run", action="store_true",
        help="Hareket algila ama kayit yapma")
    run_grp.add_argument(
        "--log-level", type=str, default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log seviyesi")

    # Notifications
    notif_grp = parser.add_argument_group("Bildirimler")
    notif_grp.add_argument(
        "--telegram-token", type=str, default=None,
        help="Telegram bot token")
    notif_grp.add_argument(
        "--telegram-chat", type=str, default=None,
        help="Telegram chat ID")
    notif_grp.add_argument(
        "--sms", type=str, default=None,
        help="SMS bildirim telefon numarasi")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # --setup: Full automated installation
    if args.setup:
        success = TermuxSetup.run_setup()
        sys.exit(0 if success else 1)

    # --check: Quick dependency check
    if args.check:
        print(f"ProSecCam v{__version__} - Bagimllik Kontrolu\n")
        issues = TermuxSetup.quick_check()
        if not issues:
            print("[OK] Tum bagimliliklar hazir!")
            print("Baslatmak icin: python ProSecCam.py")
        else:
            print("Eksik bagimliliklar:")
            for issue in issues:
                print(f"  - {issue}")
            print("\nKurulum icin: python ProSecCam.py --setup")
        sys.exit(0 if not issues else 1)

    # --init-config: Generate default config
    if args.init_config:
        config_path = args.config or "proseccam_config.json"
        Config.save_default(config_path)
        print(f"Config yazildi: {config_path}")
        print("Duzenleyin ve tekrar calistirin.")
        return

    # Normal startup
    config = Config(config_path=args.config, cli_args=args)
    setup_logging(config)

    logger = logging.getLogger("main")
    logger.info("ProSecCam v%s", __version__)

    # Auto dependency check on startup
    issues = TermuxSetup.quick_check()
    if issues:
        logger.warning("Eksik bagimliliklar tespit edildi:")
        for issue in issues:
            logger.warning("  - %s", issue)
        logger.warning("Kurulum: python ProSecCam.py --setup")
        print("\n[UYARI] Eksik bagimliliklar var. --setup ile kurun.")
        print("Devam etmek icin Enter'a basin, cikmak icin Ctrl+C...")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            return

    app = ProSecCam(config)
    app.run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
