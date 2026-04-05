#!/data/data/com.termux/files/usr/bin/python3
"""
ProSecCam - Professional Motion-Detection Security Camera for Termux
Uses Termux:API (CameraPhoto, Torch, MicrophoneRecord) and OpenCV for motion detection.
"""

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
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ProSecCamError(Exception):
    pass

class CaptureError(ProSecCamError):
    pass

class MotionDetectionError(ProSecCamError):
    pass

class RecordingError(ProSecCamError):
    pass

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict = {
    "camera_id": 0,
    "capture_interval": 2.0,
    "motion": {
        "threshold": 0.02,
        "min_contour_area": 500,
        "blur_kernel_size": 21,
        "confirmation_frames": 3,
        "confirmation_required": 2,
        "roi": None,
    },
    "recording": {
        "min_duration_seconds": 60,
        "max_duration_seconds": 300,
        "photo_interval": 0.3,
        "include_audio": True,
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
        "send_photo": True,
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

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    """Loads configuration from JSON file, overlays CLI args, validates."""

    def __init__(self, config_path: Optional[str] = None,
                 cli_args: Optional[argparse.Namespace] = None):
        self._data: Dict = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
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

# ---------------------------------------------------------------------------
# TorchController
# ---------------------------------------------------------------------------

class TorchController:
    """Manages flashlight state with tracking."""

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

    def off(self) -> None:
        with self._lock:
            if not self._is_on:
                return
            self._run("off")
            self._is_on = False

    def ensure_off(self) -> None:
        with self._lock:
            self._run("off")
            self._is_on = False

    @staticmethod
    def _run(state: str) -> None:
        try:
            subprocess.run(["termux-torch", state],
                           timeout=5, capture_output=True)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# ResourceGuard
# ---------------------------------------------------------------------------

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
                subprocess.run(["termux-wake-lock"], timeout=5,
                               capture_output=True)
                self._wake_locked = True
                self._logger.info("Wake lock acquired")
            except Exception as e:
                self._logger.warning("Failed to acquire wake lock: %s", e)

    def release_wake_lock(self) -> None:
        if self._wake_locked:
            try:
                subprocess.run(["termux-wake-unlock"], timeout=5,
                               capture_output=True)
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
        # Stop any lingering microphone recording
        try:
            subprocess.run(["termux-microphone-record", "-q"],
                           timeout=5, capture_output=True)
        except Exception:
            pass
        for cb in self._cleanup_callbacks:
            try:
                cb()
            except Exception as e:
                self._logger.warning("Cleanup callback error: %s", e)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleanup_all()

# ---------------------------------------------------------------------------
# BatteryMonitor
# ---------------------------------------------------------------------------

class BatteryMonitor:
    """Periodically checks battery via termux-battery-status."""

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
                ["termux-battery-status"], timeout=10,
                capture_output=True, text=True
            )
            data = json.loads(result.stdout)
            status = BatteryStatus(
                percentage=int(data.get("percentage", 100)),
                plugged=str(data.get("plugged", "UNPLUGGED")),
                temperature=float(data.get("temperature", 25.0)),
                status=str(data.get("status", "UNKNOWN")),
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

# ---------------------------------------------------------------------------
# StorageManager
# ---------------------------------------------------------------------------

class StorageManager:
    """Manages recording storage, cleanup, and directory structure."""

    def __init__(self, config: Config):
        self._base_path = config.get("storage.base_path",
                                     "/data/data/com.termux/files/home/proseccam")
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
        self._logger.info("Storage initialized at %s", self._base_path)

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
            self._logger.info("Deleted event for space: %s (freed %d KB)",
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
        for dirpath, _dirnames, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total

# ---------------------------------------------------------------------------
# CameraCapture
# ---------------------------------------------------------------------------

class CameraCapture:
    """Wraps termux-camera-photo with timeout and retry."""

    def __init__(self, config: Config):
        self._camera_id = config.get("camera_id", 0)
        self._timeout = 15.0
        self._logger = logging.getLogger("camera")

    def capture(self, output_path: str) -> bool:
        try:
            proc = subprocess.run(
                ["termux-camera-photo", "-c", str(self._camera_id), output_path],
                timeout=self._timeout, capture_output=True, text=True,
            )
            # Wait briefly for file to be written
            for _ in range(20):
                if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
                    return True
                time.sleep(0.25)
            self._logger.warning("Photo file not found after capture: %s",
                                 output_path)
            return False
        except subprocess.TimeoutExpired:
            self._logger.error("Camera capture timed out")
            raise CaptureError("Camera capture timed out")
        except Exception as e:
            self._logger.error("Camera capture failed: %s", e)
            raise CaptureError(str(e))

    def capture_burst(self, output_dir: str, count: int, interval: float,
                      stop_event: threading.Event) -> List[str]:
        files: List[str] = []
        for i in range(count):
            if stop_event.is_set():
                break
            fname = os.path.join(output_dir, f"frame_{i:04d}.jpg")
            try:
                if self.capture(fname):
                    files.append(fname)
            except CaptureError:
                self._logger.warning("Burst frame %d failed, continuing", i)
            if not stop_event.is_set() and i < count - 1:
                stop_event.wait(timeout=interval)
        return files

# ---------------------------------------------------------------------------
# MotionDetector
# ---------------------------------------------------------------------------

class MotionDetector:
    """OpenCV-based motion detection with adaptive thresholding."""

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
        self._bg: Optional[np.ndarray] = None
        self._alpha = 0.05
        self._calibrated = False
        self._baseline_noise = 0.0
        self._logger = logging.getLogger("motion")

    def calibrate(self, frames: List[np.ndarray]) -> None:
        if len(frames) < 2:
            self._logger.warning("Not enough frames for calibration, using defaults")
            self._calibrated = True
            return
        processed = [self._preprocess(f) for f in frames]
        # Initialize background as mean of all frames
        self._bg = np.mean(processed, axis=0).astype(np.float32)
        # Measure inter-frame differences
        diffs = []
        for i in range(len(processed) - 1):
            diff = cv2.absdiff(processed[i], processed[i + 1])
            score = np.sum(diff > 25) / diff.size
            diffs.append(score)
        self._baseline_noise = float(np.mean(diffs)) if diffs else 0.0
        effective = max(self._threshold, self._baseline_noise * 2.5)
        self._logger.info(
            "Calibration complete: baseline_noise=%.4f, effective_threshold=%.4f",
            self._baseline_noise, effective)
        self._threshold = effective
        self._calibrated = True

    def detect(self, frame: np.ndarray) -> MotionResult:
        gray = self._preprocess(frame)
        if self._bg is None:
            self._bg = gray.astype(np.float32)
            return MotionResult(False, 0.0, 0, 0, [])

        bg_uint8 = self._bg.astype(np.uint8)
        diff = cv2.absdiff(gray, bg_uint8)
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=2)

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

    def confirm_motion(self, capture_func: Callable[[], Optional[np.ndarray]]
                       ) -> Tuple[bool, Optional[str]]:
        """Capture confirmation frames and verify motion.
        capture_func should capture a photo and return the frame as numpy array,
        or None on failure. Returns (confirmed, trigger_frame_path).
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
        self._logger.info("Motion confirmation: %d/%d positive -> %s",
                          positives, self._conf_frames,
                          "CONFIRMED" if confirmed else "rejected")
        return confirmed, None

    def reset(self) -> None:
        self._bg = None
        self._calibrated = False

    def _preprocess(self, frame: np.ndarray) -> np.ndarray:
        resized = cv2.resize(frame, (self.PROCESS_WIDTH, self.PROCESS_HEIGHT))
        if len(resized.shape) == 3:
            gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        else:
            gray = resized
        blurred = cv2.GaussianBlur(
            gray, (self._blur_kernel, self._blur_kernel), 0)
        return blurred

# ---------------------------------------------------------------------------
# RecordingManager
# ---------------------------------------------------------------------------

class RecordingManager:
    """Coordinates photo burst, audio recording, and ffmpeg encoding."""

    def __init__(self, config: Config, camera: CameraCapture,
                 storage: StorageManager, torch: TorchController):
        self._min_duration = config.get("recording.min_duration_seconds", 60)
        self._max_duration = config.get("recording.max_duration_seconds", 300)
        self._photo_interval = config.get("recording.photo_interval", 0.3)
        self._include_audio = config.get("recording.include_audio", True)
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

        # Night mode torch
        if self._night_mode and self._torch_on_capture:
            self._torch.on()

        # Photo burst
        start_time = time.time()
        max_frames = int(self._max_duration / max(self._photo_interval, 0.1))
        photo_files = []
        try:
            for i in range(max_frames):
                if self._stop_event.is_set():
                    elapsed = time.time() - start_time
                    if elapsed >= self._min_duration:
                        break
                fname = os.path.join(event_dir, f"frame_{i:04d}.jpg")
                try:
                    if self._camera.capture(fname):
                        photo_files.append(fname)
                except CaptureError:
                    self._logger.warning("Frame %d capture failed", i)

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
            video_path = self._encode_video(event_dir, photo_files,
                                            audio_path if self._include_audio else None)
            if video_path:
                # Remove individual frames to save space
                for pf in photo_files:
                    try:
                        os.remove(pf)
                    except OSError:
                        pass

        # Write metadata
        metadata = {
            "timestamp": timestamp,
            "duration_seconds": round(duration, 1),
            "frame_count": len(photo_files),
            "has_video": video_path is not None,
            "has_audio": self._include_audio,
        }
        meta_path = os.path.join(event_dir, "metadata.json")
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
        except OSError as e:
            self._logger.warning("Failed to write metadata: %s", e)

        self._recording = False
        return event_dir

    def _start_audio(self, output_path: str) -> None:
        try:
            self._audio_process = subprocess.Popen(
                ["termux-microphone-record", "-f", output_path, "-e", "aac"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._logger.info("Audio recording started")
        except Exception as e:
            self._logger.warning("Failed to start audio: %s", e)
            self._audio_process = None

    def _stop_audio(self) -> None:
        try:
            subprocess.run(["termux-microphone-record", "-q"],
                           timeout=10, capture_output=True)
            self._logger.info("Audio recording stopped")
        except Exception as e:
            self._logger.warning("Failed to stop audio: %s", e)
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
        # Calculate actual framerate from capture timestamps
        if len(photo_files) < 2:
            return None
        try:
            t_first = os.path.getmtime(photo_files[0])
            t_last = os.path.getmtime(photo_files[-1])
            elapsed = t_last - t_first
            if elapsed > 0:
                fps = round(len(photo_files) / elapsed, 1)
            else:
                fps = 3.0
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
            result = subprocess.run(cmd, timeout=120, capture_output=True,
                                    text=True)
            if result.returncode == 0 and os.path.isfile(output):
                self._logger.info("Video encoded: %s (%.1f fps)", output, fps)
                return output
            else:
                self._logger.error("ffmpeg failed: %s", result.stderr[:500])
                return None
        except subprocess.TimeoutExpired:
            self._logger.error("ffmpeg encoding timed out")
            return None
        except FileNotFoundError:
            self._logger.error("ffmpeg not found. Install with: pkg install ffmpeg")
            return None

    @property
    def is_recording(self) -> bool:
        return self._recording

# ---------------------------------------------------------------------------
# NotificationManager
# ---------------------------------------------------------------------------

class NotificationManager:
    """Sends alerts via Telegram, SMS, and Termux notifications."""

    def __init__(self, config: Config):
        self._telegram_token = config.get("notifications.telegram_bot_token")
        self._telegram_chat_id = config.get("notifications.telegram_chat_id")
        self._sms_number = config.get("notifications.sms_number")
        self._send_photo = config.get("notifications.send_photo", True)
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
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"🚨 Hareket Algılandı!\nZaman: {timestamp}\nSkor: {motion_score:.4f}"

        # Termux notification (always)
        self._queue.put((self._show_notification,
                         ("Hareket Algılandı!", msg), {}))

        # Telegram
        if self._telegram_token and self._telegram_chat_id:
            self._queue.put((self._send_telegram_message, (msg,), {}))
            if self._send_photo and photo_path and os.path.isfile(photo_path):
                self._queue.put((self._send_telegram_photo,
                                 (photo_path, f"Hareket: {timestamp}"), {}))

        # SMS
        if self._sms_number:
            self._queue.put((self._send_sms, (msg,), {}))

    def notify_status(self, message: str) -> None:
        self._queue.put((self._show_notification,
                         ("ProSecCam", message), {}))
        if self._telegram_token and self._telegram_chat_id:
            self._queue.put((self._send_telegram_message, (message,), {}))

    def _send_telegram_message(self, text: str) -> bool:
        try:
            url = (f"https://api.telegram.org/bot{self._telegram_token}"
                   f"/sendMessage")
            data = json.dumps({
                "chat_id": self._telegram_chat_id,
                "text": text,
            })
            subprocess.run(
                ["curl", "-s", "-X", "POST", url,
                 "-H", "Content-Type: application/json",
                 "-d", data],
                timeout=30, capture_output=True,
            )
            return True
        except Exception as e:
            self._logger.warning("Telegram message failed: %s", e)
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
        try:
            subprocess.run(
                ["termux-sms-send", "-n", self._sms_number, message],
                timeout=15, capture_output=True,
            )
            return True
        except Exception as e:
            self._logger.warning("SMS failed: %s", e)
            return False

    @staticmethod
    def _show_notification(title: str, content: str) -> None:
        try:
            subprocess.run(
                ["termux-notification",
                 "--title", title,
                 "--content", content[:256]],
                timeout=10, capture_output=True,
            )
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=10)

# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------

class CamState(enum.Enum):
    INITIALIZING = "initializing"
    IDLE = "idle"
    DETECTING = "detecting"
    RECORDING = "recording"
    COOLDOWN = "cooldown"
    LOW_BATTERY = "low_battery"
    PAUSED = "paused"
    SHUTTING_DOWN = "shutting_down"

# ---------------------------------------------------------------------------
# ProSecCam - Main Application
# ---------------------------------------------------------------------------

class ProSecCam:
    """Main application with state machine."""

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
        self._recorder = RecordingManager(config, self._camera,
                                          self._storage, self._torch)
        self._notifier = NotificationManager(config)

        # Timing
        self._capture_interval = config.get("capture_interval", 2.0)
        self._low_capture_interval = config.get(
            "battery.low_capture_interval", 10.0)
        self._cooldown_seconds = config.get("cooldown_seconds", 15)
        self._cooldown_start: float = 0

        # Logger
        self._logger = logging.getLogger("proseccam")

    def run(self, dry_run: bool = False) -> None:
        self._dry_run = dry_run
        self._running = True

        # Signal handlers
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
                        self._logger.error("Capture failed: %s. Retrying in 5s.", e)
                        time.sleep(5)
                    except MotionDetectionError as e:
                        self._logger.error("Detection error: %s. Resetting.", e)
                        self._detector.reset()
                    except RecordingError as e:
                        self._logger.error("Recording error: %s", e)
                        self._recorder.stop()
                        self._transition(CamState.COOLDOWN)
                        self._cooldown_start = time.time()
                    except Exception as e:
                        self._logger.critical("Unexpected: %s", e, exc_info=True)
                        time.sleep(10)
            finally:
                self._notifier.notify_status("ProSecCam durduruluyor...")
                self._notifier.stop()
                self._logger.info("ProSecCam stopped.")

    def _initialize(self) -> None:
        self._logger.info("=" * 50)
        self._logger.info("ProSecCam starting...")
        self._storage.initialize()
        self._guard.acquire_wake_lock()

        # Check battery
        bat = self._battery.force_check()
        self._logger.info("Battery: %d%% (%s), Temp: %.1f°C",
                          bat.percentage, bat.plugged, bat.temperature)

        # Start notification worker
        self._notifier.start()
        self._notifier.notify_status(
            f"ProSecCam başlatılıyor... Pil: {bat.percentage}%")

        # Calibration
        self._logger.info("Calibrating motion detector...")
        temp_dir = self._storage.get_temp_dir()
        frames: List[np.ndarray] = []
        for i in range(10):
            if not self._running:
                return
            fpath = os.path.join(temp_dir, f"cal_{i}.jpg")
            try:
                if self._camera.capture(fpath):
                    frame = cv2.imread(fpath)
                    if frame is not None:
                        frames.append(frame)
            except CaptureError:
                pass
            time.sleep(2)

        self._detector.calibrate(frames)
        self._storage.clear_temp()

        self._notifier.notify_status("ProSecCam hazır - izleme başladı.")
        self._logger.info("Initialization complete. Entering IDLE state.")
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
        # Battery check
        bat = self._battery.check()
        if self._battery.is_overheating:
            self._logger.warning("Overheating! Temp: %.1f°C", bat.temperature)
            self._transition(CamState.LOW_BATTERY)
            return
        if self._battery.is_low:
            self._logger.warning("Low battery: %d%%", bat.percentage)
            self._transition(CamState.LOW_BATTERY)
            return

        # Storage cleanup
        self._storage.cleanup_if_needed()

        # Capture and detect
        result = self._capture_and_detect()
        if result and result.detected:
            self._logger.info("Motion suspected! Score: %.4f", result.score)
            self._transition(CamState.DETECTING)
            return

        time.sleep(self._capture_interval)

    def _tick_detecting(self) -> None:
        temp_dir = self._storage.get_temp_dir()

        def capture_frame() -> Optional[np.ndarray]:
            fpath = os.path.join(temp_dir, "confirm.jpg")
            try:
                if self._camera.capture(fpath):
                    frame = cv2.imread(fpath)
                    return frame
            except CaptureError:
                pass
            return None

        confirmed, _ = self._detector.confirm_motion(capture_frame)
        if confirmed:
            self._logger.info("Motion CONFIRMED! Starting recording...")
            if self._dry_run:
                self._logger.info("[DRY RUN] Would start recording")
                self._transition(CamState.COOLDOWN)
                self._cooldown_start = time.time()
            else:
                self._transition(CamState.RECORDING)
        else:
            self._logger.info("False alarm. Returning to IDLE.")
            self._transition(CamState.IDLE)

    def _tick_recording(self) -> None:
        trigger_path = os.path.join(self._storage.get_temp_dir(), "confirm.jpg")
        if not os.path.isfile(trigger_path):
            trigger_path = None

        event_dir = self._recorder.start(trigger_frame_path=trigger_path)

        # Send notification
        if event_dir:
            photo = os.path.join(event_dir, "trigger.jpg")
            if not os.path.isfile(photo):
                # Use first frame as photo
                for f in sorted(os.listdir(event_dir)):
                    if f.endswith(".jpg"):
                        photo = os.path.join(event_dir, f)
                        break
            self._notifier.notify_motion(
                event_dir, photo_path=photo if os.path.isfile(photo) else None)

        self._transition(CamState.COOLDOWN)
        self._cooldown_start = time.time()

    def _tick_cooldown(self) -> None:
        elapsed = time.time() - self._cooldown_start
        if elapsed >= self._cooldown_seconds:
            # Check battery before returning
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
            self._logger.warning("Critical battery: %d%%. Pausing.", bat.percentage)
            self._notifier.notify_status(
                f"Kritik pil seviyesi: {bat.percentage}%. Duraklatılıyor.")
            self._transition(CamState.PAUSED)
            return

        if self._battery.is_recovered and not self._battery.is_overheating:
            self._logger.info("Battery recovered: %d%%. Resuming.", bat.percentage)
            self._transition(CamState.IDLE)
            return

        # Reduced rate detection
        self._storage.cleanup_if_needed()
        result = self._capture_and_detect()
        if result and result.detected:
            self._logger.info("Motion in low-battery mode! Score: %.4f",
                              result.score)
            self._transition(CamState.DETECTING)
            return

        time.sleep(self._low_capture_interval)

    def _tick_paused(self) -> None:
        time.sleep(30)
        bat = self._battery.force_check()
        self._logger.info("Paused. Battery: %d%% (%s)", bat.percentage, bat.plugged)
        if self._battery.is_recovered or self._battery.is_charging:
            self._logger.info("Resuming from pause.")
            self._notifier.notify_status(
                f"Pil yeterli: {bat.percentage}%. Devam ediliyor.")
            self._transition(CamState.IDLE)

    def _capture_and_detect(self) -> Optional[MotionResult]:
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
        self._logger.info("Received %s. Shutting down...", sig_name)
        self._running = False
        if self._recorder.is_recording:
            self._recorder.stop()
        self._state = CamState.SHUTTING_DOWN

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

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

    # File handler
    fh = RotatingFileHandler(
        log_path,
        maxBytes=config.get("logging.max_bytes", 5242880),
        backupCount=config.get("logging.backup_count", 3),
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

# ---------------------------------------------------------------------------
# CLI & Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ProSecCam - Professional Motion-Detection Security Camera",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --init-config              Generate default config file
  %(prog)s --dry-run                  Test motion detection without recording
  %(prog)s --camera 1 --night-mode    Use front camera with torch
  %(prog)s --telegram-token TOKEN --telegram-chat CHAT_ID
        """,
    )
    parser.add_argument("--config", type=str, default=None,
                        help="Path to config JSON file")
    parser.add_argument("--camera", type=int, default=None,
                        help="Camera ID (0=rear, 1=front)")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Motion detection threshold (0.0-1.0)")
    parser.add_argument("--no-audio", action="store_true", default=None,
                        help="Disable audio recording")
    parser.add_argument("--night-mode", action="store_true", default=None,
                        help="Enable night mode with torch")
    parser.add_argument("--telegram-token", type=str, default=None,
                        help="Telegram bot token")
    parser.add_argument("--telegram-chat", type=str, default=None,
                        help="Telegram chat ID")
    parser.add_argument("--sms", type=str, default=None,
                        help="SMS alert phone number")
    parser.add_argument("--log-level", type=str, default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect motion but don't record")
    parser.add_argument("--init-config", action="store_true",
                        help="Generate default config file and exit")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.init_config:
        config_path = args.config or "proseccam_config.json"
        Config.save_default(config_path)
        print(f"Default config written to: {config_path}")
        print("Edit it and run again without --init-config")
        return

    config = Config(config_path=args.config, cli_args=args)
    setup_logging(config)

    logger = logging.getLogger("main")
    logger.info("ProSecCam v1.0")

    app = ProSecCam(config)
    app.run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
