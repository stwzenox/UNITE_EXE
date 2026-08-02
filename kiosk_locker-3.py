"""
================================================================================
  ENTERPRISE KIOSK SECURITY & APPLICATION LOCK SYSTEM
  Version: 4.0.0 (Exam Mode — URL: 192.168.0.141:5000 — U×5 Unlock)
  Platform: Windows 7 / 10 / 11 / 12
  Python: 3.8+ (3.11+ recommended)

================================================================================

INSTALL DEPENDENCIES:
  pip install keyboard pywin32 pygetwindow psutil pillow

RUN AS ADMINISTRATOR:
  Right-click the file -> "Run as administrator"
  OR open an elevated CMD and run: python kiosk_locker.py

BUILD EXE:
  pip install pyinstaller
  pyinstaller --onefile --noconsole --uac-admin --icon=lock.ico kiosk_locker.py

HOW IT WORKS:
  1. Run the script as Administrator before the exam starts.
  2. Chrome opens automatically to http://192.168.0.141:5000 in full screen.
  3. The keyboard is locked — students cannot Alt+Tab, close Chrome, or type
     anything outside the exam website.
  4. The lock-screen overlay is HIDDEN while Chrome is running so students
     see only the exam page. If Chrome crashes, the lock screen appears
     and Chrome auto-restarts.
  5. When the exam is over, the TEACHER presses U five times quickly
     (within 3 seconds). The keyboard unlocks immediately, Chrome stays
     open, and the system returns to normal.

CHANGE UNLOCK KEY / COUNT:
  Edit Config.UNLOCK_HOTKEY and Config.UNLOCK_PRESS_COUNT below.

CHANGE PASSWORD (default: 1441 — numeric only, used on lock screen):
  python -c "import hashlib; print(hashlib.sha256(b'NewPIN').hexdigest())"
  Paste the result into Config.ADMIN_PASSWORD_HASH below.

CHANGE URL:
  Edit Config.TARGET_URL below.

================================================================================
"""

# =============================================================================
#  IMPORTS
# =============================================================================

import os
import sys
import time
import struct
import signal
import atexit
import hashlib
import logging
import platform
import threading
import traceback
import subprocess
import ctypes
import ctypes.wintypes
import shutil
import glob
from datetime import datetime

import tkinter as tk
from tkinter import font as tkfont

# ---------------------------------------------------------------------------
# Third-party dependency check — show a helpful message before crashing
# ---------------------------------------------------------------------------
try:
    import keyboard
    import psutil
    import win32gui
    import win32con
    import win32api
    import win32process
    import winreg
    import pygetwindow as gw
except ImportError as e:
    ctypes.windll.user32.MessageBoxW(
        0,
        f"Missing dependency: {e}\n\n"
        "Run the following command and try again:\n\n"
        "  pip install keyboard pywin32 pygetwindow psutil pillow",
        "Kiosk Locker - Missing Dependencies",
        0x10,
    )
    sys.exit(1)


# =============================================================================
#  WINDOWS VERSION DETECTION
# =============================================================================

_WIN_VER      = int(platform.version().split(".")[0])
_IS_WIN7      = _WIN_VER < 10
_IS_WIN10_PLUS = _WIN_VER >= 10


# =============================================================================
#  CONFIGURATION MANAGER
# =============================================================================

class Config:
    """
    Central configuration for the entire kiosk system.
    Edit values here only — no changes needed anywhere else in the code.
    """

    # -------------------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------------------
    # Current password: 1441  (numeric digits only — enforced in the UI)
    # To change: python -c "import hashlib; print(hashlib.sha256(b'NewPass').hexdigest())"
    # Paste the result below.
    ADMIN_PASSWORD_HASH: str = hashlib.sha256(b"1441").hexdigest()

    MAX_FAILED_ATTEMPTS: int    = 5     # Attempts before lockout triggers
    LOCKOUT_SECONDS: int        = 30    # Lockout duration in seconds
    WRONG_PASSWORD_DELAY: float = 1.5   # Pause (s) injected after a wrong attempt

    # -------------------------------------------------------------------------
    # End-of-Exam Unlock Hotkey
    # -------------------------------------------------------------------------
    # Press this key N times quickly to unlock the keyboard (teacher trigger)
    UNLOCK_HOTKEY: str       = "u"          # key name as recognised by `keyboard`
    UNLOCK_PRESS_COUNT: int  = 5            # how many times to press it
    UNLOCK_PRESS_WINDOW: float = 3.0        # seconds window to hit all presses

    # -------------------------------------------------------------------------
    # Target Application
    # -------------------------------------------------------------------------
    TARGET_APP_NAME: str       = "chrome.exe"
    TARGET_WINDOW_KEYWORD: str = "Chrome"
    TARGET_URL: str            = "http://192.168.0.141:5000"

    # -------------------------------------------------------------------------
    # Chrome Launch Flags
    # -------------------------------------------------------------------------
    ENABLE_KIOSK_MODE: bool    = True
    FORCE_FULLSCREEN: bool     = True
    AUTO_RESTART_CHROME: bool  = True

    # Checked in order; first existing path wins
    CHROME_PATHS: list = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
        # Canary / dev channel
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome SxS\Application\chrome.exe"),
    ]

    # -------------------------------------------------------------------------
    # Watchdog
    # -------------------------------------------------------------------------
    WATCHDOG_INTERVAL: float = 0.1   # Seconds between enforcement cycles (100 ms)

    # -------------------------------------------------------------------------
    # Registry Security
    # -------------------------------------------------------------------------
    DISABLE_WINKEY_REGISTRY: bool  = True    # Block Win key via Scancode Map
    DISABLE_TASKMGR_REGISTRY: bool = False   # Optionally disable Task Manager

    # -------------------------------------------------------------------------
    # UI and Sound
    # -------------------------------------------------------------------------
    ENABLE_SOUND: bool   = True
    ENABLE_LOGGING: bool = True

    # -------------------------------------------------------------------------
    # Windows 7 compatibility tweaks (applied automatically)
    # -------------------------------------------------------------------------
    if _IS_WIN7:
        ENABLE_KIOSK_MODE = False   # Old Chrome builds may not support --kiosk


# =============================================================================
#  LOGGING SYSTEM
# =============================================================================

def _setup_logging() -> logging.Logger:
    log = logging.getLogger("KioskLocker")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    if Config.ENABLE_LOGGING:
        try:
            fh = logging.FileHandler("kiosk_locker.log", encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except Exception:
            pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log

log = _setup_logging()


# =============================================================================
#  ADMIN PRIVILEGE MODULE
# =============================================================================

class AdminPrivilegeManager:
    """Detects and requests Windows UAC administrator elevation."""

    @staticmethod
    def is_admin() -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    @staticmethod
    def request_elevation():
        """Re-launch this script with a UAC elevation prompt."""
        log.warning("Not running as Administrator - requesting elevation.")
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, " ".join(sys.argv), None, 1
        )
        sys.exit(0)

    @staticmethod
    def enforce_admin():
        if not AdminPrivilegeManager.is_admin():
            result = ctypes.windll.user32.MessageBoxW(
                0,
                "This application requires Administrator privileges.\n\n"
                "Click OK to restart with elevated permissions,\n"
                "or Cancel to exit.",
                "Kiosk Locker - Elevation Required",
                0x31,
            )
            if result == 1:
                AdminPrivilegeManager.request_elevation()
            else:
                sys.exit(0)
        log.info(f"Running as Administrator OK  (Windows {platform.version()})")


# =============================================================================
#  WIN KEY REGISTRY MANAGER
# =============================================================================

class WinKeyManager:
    """
    Temporarily disables the Windows key using the Scancode Map registry entry.

    Registry path : HKLM\\SYSTEM\\CurrentControlSet\\Control\\Keyboard Layout
    Value name    : Scancode Map
    Compatibility : Windows 7 / 10 / 11 / 12

    Lifecycle
    ---------
    disable()  -> snapshot current value -> write disable map
    restore()  -> write back exact original value
                  (or delete the entry if it did not exist before)

    Safety: atexit ensures restore() runs even on an unexpected crash.
    """

    _REG_PATH = r"SYSTEM\CurrentControlSet\Control\Keyboard Layout"
    _REG_KEY  = "Scancode Map"

    # Binary scancode map that disables:
    #   Left  Win key  scancode 0xE05B
    #   Right Win key  scancode 0xE05C
    #
    # Format: header(8 bytes) + N remap entries(4 bytes each) + null(4 bytes)
    # Destination scancode 0x0000 means "disable".
    _DISABLE_MAP = struct.pack(
        "<IIIIII",
        0x00000000,   # version  (always 0)
        0x00000000,   # flags    (always 0)
        0x00000003,   # entry count: 2 remaps + 1 null terminator
        0x005BE000,   # Left  Win (0xE05B) -> 0x0000 (disabled)
        0x005CE000,   # Right Win (0xE05C) -> 0x0000 (disabled)
        0x00000000,   # null terminator
    )

    def __init__(self):
        self._original_value: bytes = None
        self._original_existed: bool = False
        self._is_disabled: bool = False
        # Safety net: always restore on interpreter exit
        atexit.register(self.restore)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _open_key(self, write: bool = False):
        access = winreg.KEY_READ
        if write:
            access = winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
        return winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            self._REG_PATH,
            0,
            access,
        )

    def _read_current(self):
        """Return (value_bytes_or_None, existed_bool)."""
        try:
            key = self._open_key(write=False)
            value, _ = winreg.QueryValueEx(key, self._REG_KEY)
            winreg.CloseKey(key)
            return value, True
        except FileNotFoundError:
            return None, False
        except Exception as e:
            log.warning(f"WinKeyManager: registry read error: {e}")
            return None, False

    def _write_value(self, data: bytes) -> bool:
        try:
            key = self._open_key(write=True)
            winreg.SetValueEx(key, self._REG_KEY, 0, winreg.REG_BINARY, data)
            winreg.CloseKey(key)
            return True
        except PermissionError:
            log.error(
                "WinKeyManager: PermissionError - "
                "Administrator privileges are required to write HKLM."
            )
            return False
        except Exception as e:
            log.error(f"WinKeyManager: registry write error: {e}")
            return False

    def _delete_value(self) -> bool:
        """Remove the Scancode Map entry entirely (factory state)."""
        try:
            key = self._open_key(write=True)
            winreg.DeleteValue(key, self._REG_KEY)
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            return True   # already absent - correct state
        except Exception as e:
            log.error(f"WinKeyManager: registry delete error: {e}")
            return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def disable(self) -> bool:
        """
        Snapshot the current Scancode Map (if any) then write the
        Win-key-disabling map.

        Note: Full registry effect requires a logoff/reboot.
              The keyboard hook provides immediate suppression in the
              current session.
        """
        if not Config.DISABLE_WINKEY_REGISTRY:
            log.info("WinKeyManager: disabled in Config - skipping.")
            return True

        if self._is_disabled:
            log.debug("WinKeyManager: already disabled.")
            return True

        # Save snapshot
        self._original_value, self._original_existed = self._read_current()

        if self._original_existed:
            log.info(
                f"WinKeyManager: snapshot saved "
                f"({len(self._original_value)} bytes)."
            )
        else:
            log.info(
                "WinKeyManager: no prior Scancode Map found - "
                "will delete entry on restore."
            )

        # Write the disable map
        success = self._write_value(self._DISABLE_MAP)
        if success:
            self._is_disabled = True
            log.info("WinKeyManager: Windows key DISABLED via Scancode Map OK")
        return success

    def restore(self) -> bool:
        """
        Restore the registry to exactly its pre-disable state:
          - If a value existed before  -> write back those exact bytes
          - If no value existed before -> delete the entry
        """
        if not self._is_disabled:
            return True

        if self._original_existed and self._original_value is not None:
            success = self._write_value(self._original_value)
            if success:
                log.info(
                    f"WinKeyManager: Scancode Map restored to original "
                    f"({len(self._original_value)} bytes) OK"
                )
        else:
            success = self._delete_value()
            if success:
                log.info(
                    "WinKeyManager: Scancode Map entry removed "
                    "(it was not set before kiosk started) OK"
                )

        if success:
            self._is_disabled = False
        return success

    @property
    def is_disabled(self) -> bool:
        return self._is_disabled


# =============================================================================
#  ADVANCED SECURITY MODULE
# =============================================================================

class AdvancedSecurityModule:
    """Optional registry-based lockdowns (Task Manager, etc.)."""

    @staticmethod
    def disable_task_manager():
        if not Config.DISABLE_TASKMGR_REGISTRY:
            return
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Policies\System",
                0,
                winreg.KEY_SET_VALUE | winreg.KEY_CREATE_SUB_KEY,
            )
            winreg.SetValueEx(key, "DisableTaskMgr", 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(key)
            log.info("Task Manager disabled via registry OK")
        except Exception as e:
            log.warning(f"Could not disable Task Manager: {e}")

    @staticmethod
    def restore_task_manager():
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Policies\System",
                0,
                winreg.KEY_SET_VALUE,
            )
            winreg.SetValueEx(key, "DisableTaskMgr", 0, winreg.REG_DWORD, 0)
            winreg.CloseKey(key)
            log.info("Task Manager re-enabled via registry OK")
        except Exception:
            pass


# =============================================================================
#  PASSWORD AUTHENTICATION MODULE
# =============================================================================

class PasswordAuth:
    """
    SHA-256 password authentication with attempt tracking and timed lockout.
    Thread-safe via internal lock.
    """

    def __init__(self):
        self.failed_attempts: int = 0
        self.locked_until: float  = 0.0
        self._lock = threading.Lock()

    def is_locked_out(self) -> bool:
        return time.time() < self.locked_until

    def seconds_remaining(self) -> int:
        return max(0, int(self.locked_until - time.time()))

    def verify(self, password: str) -> bool:
        with self._lock:
            if self.is_locked_out():
                log.warning("Auth attempt during lockout period - rejected.")
                return False

            entered_hash = hashlib.sha256(password.encode()).hexdigest()

            if entered_hash == Config.ADMIN_PASSWORD_HASH:
                self.failed_attempts = 0
                log.info("Authentication SUCCESS - kiosk unlocking.")
                return True
            else:
                self.failed_attempts += 1
                log.warning(
                    f"Authentication FAILED - "
                    f"attempt {self.failed_attempts}/{Config.MAX_FAILED_ATTEMPTS}"
                )
                if self.failed_attempts >= Config.MAX_FAILED_ATTEMPTS:
                    self.locked_until = time.time() + Config.LOCKOUT_SECONDS
                    log.warning(
                        f"Max attempts reached - "
                        f"locked out for {Config.LOCKOUT_SECONDS}s"
                    )
                return False

    def reset(self):
        with self._lock:
            self.failed_attempts = 0
            self.locked_until    = 0.0


# =============================================================================
#  KEYBOARD SECURITY MODULE
# =============================================================================

class KeyboardSecurityModule:
    """
    Low-level global keyboard suppression using keyboard.hook(suppress=True).

    All keys are swallowed system-wide EXCEPT:
      - The password entry field is temporarily exempted via allow_password_entry().
      - A secret hotkey (default: pressing 'U' 5 times within 3 seconds) triggers
        the end-of-exam unlock callback so the teacher can free the keyboard.

    A redundant per-hotkey block list provides an extra layer of protection.
    """

    _BLOCKED_HOTKEYS = [
        "alt+tab",    "alt+f4",         "ctrl+shift+esc",  "ctrl+esc",
        "win+r",      "win+d",          "win+l",           "win+tab",
        "win+e",      "win+x",          "win+i",           "win+s",
        "win+home",   "win+left",       "win+right",       "win+up",
        "win+down",   "ctrl+w",         "ctrl+t",          "ctrl+n",
        "alt+space",  "alt+enter",
        "f1",  "f2",  "f3",  "f4",  "f5",  "f6",
        "f7",  "f8",  "f9",  "f10", "f11", "f12",
    ]

    def __init__(self):
        self._active: bool       = False
        self._allow_typing: bool = False
        self._lock               = threading.Lock()

        # Secret unlock sequence state
        self._unlock_callback    = None   # set by caller via set_unlock_callback()
        self._unlock_timestamps: list = []

    def set_unlock_callback(self, cb):
        """Register the function to call when U×5 is detected."""
        self._unlock_callback = cb

    def _suppress_callback(self, event: keyboard.KeyboardEvent):
        """
        Low-level hook callback.
        Returning None suppresses the event; returning False passes it through.
        """
        if not self._active:
            return False

        # ---- Secret unlock detector (key-down events only) ---------------
        if (event.event_type == keyboard.KEY_DOWN and
                event.name and
                event.name.lower() == Config.UNLOCK_HOTKEY.lower()):
            now = time.time()
            # Keep only presses within the time window
            self._unlock_timestamps = [
                t for t in self._unlock_timestamps
                if now - t <= Config.UNLOCK_PRESS_WINDOW
            ]
            self._unlock_timestamps.append(now)
            log.debug(
                f"Unlock key '{Config.UNLOCK_HOTKEY}' pressed "
                f"({len(self._unlock_timestamps)}/{Config.UNLOCK_PRESS_COUNT})"
            )
            if len(self._unlock_timestamps) >= Config.UNLOCK_PRESS_COUNT:
                self._unlock_timestamps.clear()
                log.info(
                    f"Secret unlock sequence detected "
                    f"('{Config.UNLOCK_HOTKEY}' × {Config.UNLOCK_PRESS_COUNT}) "
                    f"— triggering end-of-exam unlock."
                )
                if self._unlock_callback:
                    threading.Thread(
                        target=self._unlock_callback,
                        daemon=True,
                        name="UnlockThread",
                    ).start()
            return None  # suppress the key itself so students can't see it typed

        # ---- Normal suppression logic -------------------------------------
        if self._allow_typing:
            return False   # pass through to password entry
        return None        # suppress everything else

    def start(self):
        with self._lock:
            if self._active:
                return
            self._active = True
            keyboard.hook(self._suppress_callback, suppress=True)
            for hotkey in self._BLOCKED_HOTKEYS:
                try:
                    keyboard.block_key(hotkey)
                except Exception:
                    pass
            log.info("Keyboard security ACTIVATED - all keys suppressed OK")

    def stop(self):
        with self._lock:
            if not self._active:
                return
            self._active = False
            try:
                keyboard.unhook_all()
            except Exception:
                pass
            log.info("Keyboard security DEACTIVATED - keys restored OK")

    def allow_password_entry(self, allow: bool):
        """Toggle keyboard pass-through for the password entry field."""
        self._allow_typing = allow
        log.debug(f"Password entry mode: {'ON' if allow else 'OFF'}")

    def is_active(self) -> bool:
        return self._active


# =============================================================================
#  APP LAUNCHER MODULE
# =============================================================================

class AppLauncherModule:
    """Locates, launches, and tracks the Chrome kiosk process."""

    def __init__(self):
        self._chrome_path: str = self._find_chrome()
        self._process          = None

    def _find_chrome(self) -> str:
        # 1) Check hard-coded / env-var paths
        for path in Config.CHROME_PATHS:
            expanded = os.path.expandvars(path)
            if os.path.isfile(expanded):
                log.info(f"Chrome found (path list): {expanded}")
                return expanded

        # 2) Search registry (HKLM and HKCU)
        reg_keys = [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        ]
        for hive, key_path in reg_keys:
            try:
                key = winreg.OpenKey(hive, key_path)
                val, _ = winreg.QueryValueEx(key, "")
                winreg.CloseKey(key)
                val = val.strip('"').strip()
                if os.path.isfile(val):
                    log.info(f"Chrome found (registry): {val}")
                    return val
            except Exception:
                pass

        # 3) Glob search under Program Files for any chrome.exe
        for base in [
            r"C:\Program Files",
            r"C:\Program Files (x86)",
            os.path.expandvars(r"%LOCALAPPDATA%"),
        ]:
            pattern = os.path.join(base, "**", "chrome.exe")
            try:
                matches = glob.glob(pattern, recursive=True)
                if matches:
                    log.info(f"Chrome found (glob): {matches[0]}")
                    return matches[0]
            except Exception:
                pass

        # 4) shutil.which (works if chrome is on PATH)
        which_path = shutil.which("chrome") or shutil.which("google-chrome")
        if which_path:
            log.info(f"Chrome found (PATH): {which_path}")
            return which_path

        log.error(
            "Chrome NOT FOUND anywhere on this system.\n"
            "Please install Google Chrome and ensure it is accessible, "
            "or manually set Config.CHROME_PATHS."
        )
        return "chrome"  # last-resort fallback — will raise FileNotFoundError on launch

    def kill_existing_chrome(self):
        """Terminate all running Chrome processes before a fresh launch."""
        killed = 0
        for proc in psutil.process_iter(["name", "pid"]):
            try:
                if proc.info["name"] and \
                   Config.TARGET_APP_NAME.lower() in proc.info["name"].lower():
                    proc.kill()
                    killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if killed:
            log.info(f"Terminated {killed} existing Chrome process(es).")
            time.sleep(1.2)

    def launch(self) -> bool:
        """Build the argument list and launch Chrome in fullscreen kiosk mode."""
        if not self._chrome_path or not os.path.isfile(self._chrome_path):
            # Try to re-discover Chrome (handles installs that happened after startup)
            self._chrome_path = self._find_chrome()

        # Dedicated user-data-dir avoids "profile already in use" errors
        user_data_dir = os.path.join(
            os.path.expandvars("%LOCALAPPDATA%"), "KioskChrome", "UserData"
        )
        os.makedirs(user_data_dir, exist_ok=True)

        # Screen dimensions for --window-size
        try:
            import ctypes
            user32 = ctypes.windll.user32
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
        except Exception:
            sw, sh = 1920, 1080

        args = [self._chrome_path]

        if Config.ENABLE_KIOSK_MODE:
            args.append("--kiosk")           # True kiosk: no title bar, no address bar
        else:
            # Fallback for Win7 / older Chrome
            args += ["--start-maximized", "--start-fullscreen"]

        args += [
            # Fullscreen geometry
            f"--window-size={sw},{sh}",
            "--window-position=0,0",
            # Navigation / UX restrictions
            "--disable-pinch",
            "--overscroll-history-navigation=0",
            "--disable-session-crashed-bubble",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--disable-translate",
            "--disable-extensions",
            "--disable-dev-tools",
            "--noerrdialogs",
            "--disable-features=TranslateUI",
            # Suppress restore-session dialog
            "--restore-last-session",
            # Allow local HTTP server (192.168.x.x:port) without security warnings
            "--allow-insecure-localhost",
            "--disable-web-security",
            "--allow-running-insecure-content",
            "--ignore-certificate-errors",
            "--ignore-ssl-errors",
            # Profile isolation (prevents "profile already open" blocking)
            f"--user-data-dir={user_data_dir}",
            "--profile-directory=Default",
            # Open a fresh window to the target URL
            "--new-window",
            Config.TARGET_URL,
        ]

        try:
            self._process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info(
                f"Chrome launched  PID={self._process.pid}  "
                f"URL={Config.TARGET_URL}  "
                f"Path={self._chrome_path}"
            )
            return True
        except FileNotFoundError:
            log.error(f"Chrome executable not found at: {self._chrome_path}")
            ctypes.windll.user32.MessageBoxW(
                0,
                f"Google Chrome could not be found.\n\n"
                f"Tried: {self._chrome_path}\n\n"
                "Please install Chrome from https://www.google.com/chrome\n"
                "or update Config.CHROME_PATHS in kiosk_locker.py.",
                "Kiosk Locker - Chrome Not Found",
                0x10,
            )
            return False
        except Exception as e:
            log.error(f"Chrome launch failed: {e}")
            return False

    def is_running(self) -> bool:
        for proc in psutil.process_iter(["name"]):
            try:
                if proc.info["name"] and \
                   Config.TARGET_APP_NAME.lower() in proc.info["name"].lower():
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return False


# =============================================================================
#  PROCESS MONITOR MODULE
# =============================================================================

class ProcessMonitorModule:
    """Background thread that detects Chrome crashes and triggers a callback."""

    def __init__(self, launcher: AppLauncherModule, on_crash_callback):
        self._launcher  = launcher
        self._on_crash  = on_crash_callback
        self._running   = False
        self._thread    = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="ProcessMonitor",
        )
        self._thread.start()
        log.info("Process monitor started OK")

    def stop(self):
        self._running = False

    def _monitor_loop(self):
        while self._running:
            try:
                if not self._launcher.is_running():
                    log.warning("Chrome not detected - triggering crash recovery.")
                    self._on_crash()
                    time.sleep(5.0)   # cooldown after relaunch attempt
            except Exception as e:
                log.error(f"Process monitor error: {e}")
            time.sleep(2.0)


# =============================================================================
#  WINDOW FOCUS ENFORCEMENT MODULE
# =============================================================================

class WindowFocusEnforcer:
    """
    Aggressively keeps Chrome as the foreground window.
    Runs every WATCHDOG_INTERVAL seconds using Win32 API.

    Uses AttachThreadInput to bypass the focus-steal prevention that
    Windows 10 and 11 introduced.
    """

    def __init__(self):
        self._running = False
        self._thread  = None

    def _find_chrome_hwnd(self):
        """Enumerate all visible windows and return the first Chrome handle."""
        results = []

        def enum_cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if Config.TARGET_WINDOW_KEYWORD in title:
                    results.append(hwnd)

        try:
            win32gui.EnumWindows(enum_cb, None)
        except Exception:
            pass
        return results[0] if results else None

    def _bring_to_front(self, hwnd: int):
        """Force Chrome to foreground and ensure it is maximized / fullscreen."""
        try:
            # Restore if minimised
            placement = win32gui.GetWindowPlacement(hwnd)
            if placement[1] == win32con.SW_SHOWMINIMIZED:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

            # Attach thread input so SetForegroundWindow succeeds on Win10/11
            fg_hwnd = win32gui.GetForegroundWindow()
            if fg_hwnd and fg_hwnd != hwnd:
                fg_tid  = win32process.GetWindowThreadProcessId(fg_hwnd, None)[0]
                cur_tid = win32api.GetCurrentThreadId()
                if fg_tid != cur_tid:
                    try:
                        win32process.AttachThreadInput(fg_tid, cur_tid, True)
                        win32gui.SetForegroundWindow(hwnd)
                        win32gui.BringWindowToTop(hwnd)
                        win32process.AttachThreadInput(fg_tid, cur_tid, False)
                    except Exception:
                        win32gui.SetForegroundWindow(hwnd)
            else:
                win32gui.SetForegroundWindow(hwnd)

            # Pin to TOPMOST z-order
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOPMOST,
                0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
            )

            # Maximise to fill the full screen
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)

            # If not in --kiosk mode, also try to cover the full screen area
            if not Config.ENABLE_KIOSK_MODE:
                try:
                    user32 = ctypes.windll.user32
                    sw = user32.GetSystemMetrics(0)
                    sh = user32.GetSystemMetrics(1)
                    win32gui.SetWindowPos(
                        hwnd,
                        win32con.HWND_TOPMOST,
                        0, 0, sw, sh,
                        win32con.SWP_SHOWWINDOW,
                    )
                except Exception:
                    pass

        except Exception as e:
            log.debug(f"Focus enforcement error: {e}")

    def _enforcer_loop(self):
        while self._running:
            try:
                hwnd = self._find_chrome_hwnd()
                if hwnd:
                    fg = win32gui.GetForegroundWindow()
                    if fg != hwnd:
                        self._bring_to_front(hwnd)
            except Exception:
                pass
            time.sleep(Config.WATCHDOG_INTERVAL)

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._enforcer_loop,
            daemon=True,
            name="WindowFocusEnforcer",
        )
        self._thread.start()
        log.info("Window focus enforcer started OK")

    def stop(self):
        self._running = False
        log.info("Window focus enforcer stopped.")


# =============================================================================
#  KIOSK OVERLAY UI MODULE
# =============================================================================

class KioskOverlayUI:
    """
    Professional fullscreen dark-mode lock overlay built with tkinter.

    Features:
      - Fullscreen, always-on-top, non-closable, hidden from Alt+Tab
      - Animated pulsing lock icon
      - Real-time clock / date
      - SHA-256 password field with lockout display
      - Failed attempt counter
      - Security status indicators
      - Red-flash feedback on wrong password
      - Windows system sound effects
    """

    # -------------------------------------------------------------------------
    # Colour palette
    # -------------------------------------------------------------------------
    BG           = "#0a0c10"
    PANEL        = "#111520"
    PANEL_BORDER = "#1e2535"
    ACCENT       = "#3b82f6"
    ACCENT2      = "#06b6d4"
    GLOW         = "#1d4ed8"
    TEXT_PRI     = "#f0f4ff"
    TEXT_SEC     = "#8899bb"
    TEXT_DIM     = "#334466"
    SUCCESS      = "#10b981"
    DANGER       = "#ef4444"
    WARN         = "#f59e0b"
    ENTRY_BG     = "#0d1117"
    ENTRY_FG     = "#e2e8f0"

    def __init__(
        self,
        auth: PasswordAuth,
        keyboard_module: KeyboardSecurityModule,
        on_unlock,
    ):
        self._auth      = auth
        self._keyboard  = keyboard_module
        self._on_unlock = on_unlock

        # Widget references populated in _build()
        self._root           = None
        self._panel          = None
        self._canvas         = None
        self._password_entry = None
        self._unlock_btn     = None
        self._status_label   = None

        # StringVars
        self._status_var     = None
        self._clock_var      = None
        self._attempts_var   = None
        self._locked_out_var = None
        self._password_var   = None

        # Animation state
        self._pulse_val = 0
        self._pulse_dir = 1

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self):
        root = tk.Tk()
        self._root = root

        root.title("Kiosk Security System")
        root.configure(bg=self.BG)
        root.attributes("-fullscreen", True)
        root.attributes("-topmost",    True)
        root.overrideredirect(True)                    # remove title bar / border
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        root.resizable(False, False)

        # Hide from Alt+Tab (WS_EX_TOOLWINDOW trick)
        try:
            hwnd     = ctypes.windll.user32.GetParent(root.winfo_id())
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                hwnd,
                win32con.GWL_EXSTYLE,
                (ex_style | win32con.WS_EX_TOOLWINDOW) & ~win32con.WS_EX_APPWINDOW,
            )
        except Exception:
            pass

        # Block all tkinter-level key events except those in the password field
        root.bind_all("<Key>", self._tk_key_handler)

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()

        self._build_dot_grid_background(root, sw, sh)
        self._build_center_panel(root, sw, sh)

        # Create StringVars
        self._status_var     = tk.StringVar(value="")
        self._clock_var      = tk.StringVar(value="")
        self._attempts_var   = tk.StringVar(value="")
        self._locked_out_var = tk.StringVar(value="")
        self._password_var   = tk.StringVar()

        self._populate_panel()

        # Start update loops
        self._animate_lock()
        self._update_clock()
        self._refresh_auth_display()

        # Auto-focus the password field after a short delay
        root.after(250, self._password_entry.focus_set)

        # Start the Chrome-presence monitor loop (hides/shows overlay)
        self._chrome_visibility_loop()

    def _chrome_visibility_loop(self):
        """
        Periodically check if Chrome is running.
        - Chrome running  → withdraw the overlay so students see only Chrome.
        - Chrome not running → show the overlay (locked screen).
        Called from the tkinter thread via root.after().
        """
        if not self._root:
            return
        try:
            chrome_alive = any(
                proc.info.get("name", "").lower() == Config.TARGET_APP_NAME.lower()
                for proc in psutil.process_iter(["name"])
            )
            if chrome_alive:
                # Hide overlay — Chrome is showing the exam page
                if self._root.state() != "withdrawn":
                    self._root.withdraw()
                    log.debug("Overlay hidden — Chrome is running.")
            else:
                # Show overlay — Chrome is gone, lock the screen
                if self._root.state() == "withdrawn":
                    self._root.deiconify()
                    self._root.attributes("-topmost", True)
                    self._root.lift()
                    self._password_entry.focus_set()
                    log.debug("Overlay shown — Chrome not running.")
        except Exception:
            pass
        # Re-schedule every 800 ms
        self._root.after(800, self._chrome_visibility_loop)

    def _build_dot_grid_background(self, root, w, h):
        bg = tk.Canvas(root, bg=self.BG, highlightthickness=0)
        bg.place(x=0, y=0, width=w, height=h)
        for x in range(0, w, 45):
            for y in range(0, h, 45):
                bg.create_oval(x - 1, y - 1, x + 1, y + 1,
                               fill=self.TEXT_DIM, outline="")

    def _build_center_panel(self, root, sw, sh):
        pw, ph = 500, 670
        px = (sw - pw) // 2
        py = (sh - ph) // 2

        # Drop shadow
        tk.Frame(root, bg="#000000").place(x=px + 8, y=py + 8, width=pw, height=ph)

        # Main panel
        panel = tk.Frame(
            root,
            bg=self.PANEL,
            highlightbackground=self.PANEL_BORDER,
            highlightthickness=1,
        )
        panel.place(x=px, y=py, width=pw, height=ph)

        # Top accent stripe
        tk.Frame(panel, bg=self.ACCENT, height=3).pack(fill="x", side="top")

        self._panel = panel

    def _populate_panel(self):
        p = self._panel

        # Helper to create fonts safely
        def F(family, size, weight="normal"):
            try:
                return tkfont.Font(family=family, size=size, weight=weight)
            except Exception:
                return None

        f_heading = F("Segoe UI",     26, "bold")
        f_sub     = F("Segoe UI",     10)
        f_app     = F("Consolas",      9)
        f_clock   = F("Segoe UI Mono",13, "bold")
        f_label   = F("Segoe UI",      9)
        f_btn     = F("Segoe UI",     11, "bold")
        f_small   = F("Segoe UI",      8)
        f_entry   = ("Consolas", 13)

        # ---- Animated lock icon -----------------------------------------
        self._canvas = tk.Canvas(
            p, bg=self.PANEL, highlightthickness=0, width=110, height=110
        )
        self._canvas.pack(pady=(28, 0))
        self._draw_lock()

        # ---- Headings ---------------------------------------------------
        tk.Label(
            p, text="SYSTEM LOCKED",
            bg=self.PANEL, fg=self.TEXT_PRI, font=f_heading,
        ).pack(pady=(10, 0))

        tk.Label(
            p, text="Restricted Kiosk Session",
            bg=self.PANEL, fg=self.TEXT_SEC, font=f_sub,
        ).pack(pady=(2, 0))

        tk.Frame(p, bg=self.PANEL_BORDER, height=1).pack(fill="x", padx=40, pady=10)

        # ---- Application status pill ------------------------------------
        pill = tk.Frame(
            p, bg="#0d1f35",
            highlightbackground=self.ACCENT2, highlightthickness=1,
        )
        pill.pack(pady=(0, 4))
        tk.Label(
            pill,
            text="  Google Chrome - Kiosk Mode Active  ",
            bg="#0d1f35", fg=self.ACCENT2, font=f_app,
            padx=14, pady=5,
        ).pack()

        # ---- Live clock -------------------------------------------------
        tk.Label(
            p, textvariable=self._clock_var,
            bg=self.PANEL, fg=self.ACCENT, font=f_clock,
        ).pack(pady=(8, 0))

        tk.Frame(p, bg=self.PANEL_BORDER, height=1).pack(fill="x", padx=40, pady=12)

        # ---- Password section -------------------------------------------
        tk.Label(
            p, text="ENTER NUMERIC PIN  (digits 0–9 only)",
            bg=self.PANEL, fg=self.TEXT_SEC, font=f_label,
        ).pack(pady=(0, 6))

        # Entry with 1 px accent border
        entry_frame = tk.Frame(p, bg=self.ACCENT, padx=1, pady=1)
        entry_frame.pack(padx=60, pady=(0, 8), fill="x")

        # Register Tk validatecommand — only digits 0-9 accepted
        vcmd = (
            self._root.register(self._validate_numeric_input),
            "%d",   # action: 1=insert, 0=delete
            "%P",   # value of the field if the edit is allowed
        )

        self._password_entry = tk.Entry(
            entry_frame,
            textvariable=self._password_var,
            show="*",
            font=f_entry,
            bg=self.ENTRY_BG,
            fg=self.ENTRY_FG,
            insertbackground=self.ACCENT,
            relief="flat",
            justify="center",
            validate="key",
            validatecommand=vcmd,
        )
        self._password_entry.pack(fill="x", ipady=9, padx=1, pady=1)
        self._password_entry.bind(
            "<FocusIn>",
            lambda e: self._keyboard.allow_password_entry(True),
        )
        self._password_entry.bind(
            "<FocusOut>",
            lambda e: self._keyboard.allow_password_entry(False),
        )
        self._password_entry.bind("<Return>", lambda e: self._attempt_unlock())

        # ---- Unlock button ----------------------------------------------
        self._unlock_btn = tk.Button(
            p,
            text="UNLOCK SYSTEM",
            command=self._attempt_unlock,
            bg=self.ACCENT,
            fg="#ffffff",
            font=f_btn,
            relief="flat",
            cursor="hand2",
            activebackground=self.GLOW,
            activeforeground="#ffffff",
            bd=0,
        )
        self._unlock_btn.pack(padx=60, pady=(0, 8), fill="x", ipady=9)
        self._unlock_btn.bind("<Enter>", lambda e: self._unlock_btn.config(bg=self.GLOW))
        self._unlock_btn.bind("<Leave>", lambda e: self._unlock_btn.config(bg=self.ACCENT))

        # ---- Status / error message -------------------------------------
        self._status_label = tk.Label(
            p,
            textvariable=self._status_var,
            bg=self.PANEL,
            fg=self.DANGER,
            font=f_label,
            wraplength=380,
        )
        self._status_label.pack(pady=(0, 2))

        # ---- Lockout timer display --------------------------------------
        tk.Label(
            p, textvariable=self._locked_out_var,
            bg=self.PANEL, fg=self.WARN, font=f_small,
        ).pack()

        # ---- Failed attempt counter -------------------------------------
        tk.Label(
            p, textvariable=self._attempts_var,
            bg=self.PANEL, fg=self.TEXT_DIM, font=f_small,
        ).pack(pady=(0, 10))

        tk.Frame(p, bg=self.PANEL_BORDER, height=1).pack(fill="x", padx=40, pady=4)

        # ---- Security status indicators ---------------------------------
        row = tk.Frame(p, bg=self.PANEL)
        row.pack(pady=8)
        for text, color in [
            ("* KEYBOARD LOCKED", self.SUCCESS),
            ("* WIN KEY BLOCKED", self.SUCCESS),
            ("* SECURE MODE",     self.SUCCESS),
        ]:
            tk.Label(
                row, text=text,
                bg=self.PANEL, fg=color, font=f_small,
            ).pack(side="left", padx=8)

        # ---- Footer -----------------------------------------------------
        tk.Label(
            p,
            text="Enterprise Kiosk Security System  |  Unauthorized access is prohibited",
            bg=self.PANEL,
            fg=self.TEXT_DIM,
            font=f_small,
        ).pack(pady=(4, 18))

    # ------------------------------------------------------------------
    # Lock icon and animation
    # ------------------------------------------------------------------

    def _draw_lock(self):
        c = self._canvas
        c.delete("all")
        cx, cy = 55, 58

        # Concentric glow rings
        for r, col in [(46, "#0d1a33"), (42, "#122244"), (38, "#172d55")]:
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=col, outline="")

        # Lock body
        c.create_rectangle(
            cx - 20, cy - 12, cx + 20, cy + 24,
            fill=self.ACCENT, outline=self.ACCENT2, width=2,
        )
        # Shackle arch
        c.create_arc(
            cx - 14, cy - 42, cx + 14, cy - 10,
            start=0, extent=180,
            style="arc", outline=self.ACCENT2, width=3,
        )
        # Keyhole circle
        c.create_oval(
            cx - 5, cy - 2, cx + 5, cy + 9,
            fill=self.PANEL, outline=self.PANEL,
        )
        # Keyhole stem
        c.create_rectangle(
            cx - 2, cy + 5, cx + 2, cy + 16,
            fill=self.PANEL, outline=self.PANEL,
        )

    def _animate_lock(self):
        if not self._root:
            return

        # Update pulse value
        self._pulse_val += self._pulse_dir * 3
        if self._pulse_val >= 100:
            self._pulse_dir = -1
        elif self._pulse_val <= 0:
            self._pulse_dir = 1

        t = self._pulse_val / 100
        r_val = int(0x10 + 0x20 * t)
        g_val = int(0x30 + 0x40 * t)
        b_val = int(0x80 + 0x60 * t)
        glow_col = f"#{r_val:02x}{g_val:02x}{b_val:02x}"

        if self._canvas:
            try:
                self._canvas.delete("pulse")
                r  = 50 + int(5 * t)
                cx, cy = 55, 58
                self._canvas.create_oval(
                    cx - r, cy - r, cx + r, cy + r,
                    fill="", outline=glow_col, width=2, tags="pulse",
                )
                self._canvas.tag_lower("pulse")
            except Exception:
                pass

        self._root.after(35, self._animate_lock)

    # ------------------------------------------------------------------
    # Clock update
    # ------------------------------------------------------------------

    def _update_clock(self):
        if not self._root:
            return
        self._clock_var.set(datetime.now().strftime("%A, %d %B %Y   %H:%M:%S"))
        self._root.after(1000, self._update_clock)

    # ------------------------------------------------------------------
    # Auth display refresh
    # ------------------------------------------------------------------

    def _refresh_auth_display(self):
        if not self._root:
            return

        if self._auth.is_locked_out():
            remaining = self._auth.seconds_remaining()
            self._locked_out_var.set(
                f"Account locked - try again in {remaining} second(s)"
            )
            try:
                self._unlock_btn.config(state="disabled", bg="#1e2535")
            except Exception:
                pass
        else:
            self._locked_out_var.set("")
            try:
                self._unlock_btn.config(state="normal", bg=self.ACCENT)
            except Exception:
                pass

        if self._auth.failed_attempts > 0 and not self._auth.is_locked_out():
            remaining_attempts = (
                Config.MAX_FAILED_ATTEMPTS - self._auth.failed_attempts
            )
            self._attempts_var.set(
                f"Failed attempts: {self._auth.failed_attempts} / "
                f"{Config.MAX_FAILED_ATTEMPTS}   "
                f"({remaining_attempts} remaining)"
            )
        else:
            self._attempts_var.set("")

        self._root.after(500, self._refresh_auth_display)

    # ------------------------------------------------------------------
    # Keyboard handler
    # ------------------------------------------------------------------

    def _tk_key_handler(self, event):
        """
        Block ALL keys at the tkinter level everywhere EXCEPT inside the
        password entry — where only digits 0-9, Backspace, and Return are
        allowed.  Every other key is silently swallowed.
        """
        if self._root and self._root.focus_get() == self._password_entry:
            # Let digits, Backspace and Return through; block everything else
            if event.keysym in (
                "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
                "BackSpace", "Return", "KP_Enter",
                # Numpad digit keys
                "KP_0", "KP_1", "KP_2", "KP_3", "KP_4",
                "KP_5", "KP_6", "KP_7", "KP_8", "KP_9",
            ):
                return          # pass through to the Entry widget
            return "break"      # swallow non-numeric key inside the entry
        return "break"          # swallow everything outside the entry

    def _validate_numeric_input(self, action, value_if_allowed):
        """
        Tkinter validatecommand callback.
        Allows the change only when every character in value_if_allowed is a digit.
        action == '1' means an insertion is being attempted.
        """
        if action == "1":       # insertion
            return value_if_allowed.isdigit()
        return True             # deletions always allowed

    # ------------------------------------------------------------------
    # Unlock logic
    # ------------------------------------------------------------------

    def _attempt_unlock(self):
        if self._auth.is_locked_out():
            remaining = self._auth.seconds_remaining()
            self._set_status(
                f"System locked. Try again in {remaining} second(s).",
                color=self.WARN,
            )
            self._play_sound("error")
            return

        password = self._password_var.get()
        self._password_var.set("")

        if self._auth.verify(password):
            self._set_status(
                "Authentication successful - restoring system...",
                color=self.SUCCESS,
            )
            self._play_sound("success")
            self._root.after(600, self._on_unlock)
        else:
            if self._auth.is_locked_out():
                self._set_status(
                    f"Too many failed attempts. "
                    f"Locked for {Config.LOCKOUT_SECONDS} seconds.",
                    color=self.WARN,
                )
            else:
                remaining = Config.MAX_FAILED_ATTEMPTS - self._auth.failed_attempts
                self._set_status(
                    f"Incorrect password. {remaining} attempt(s) remaining.",
                    color=self.DANGER,
                )
            self._play_sound("error")
            self._flash_border()

    def _set_status(self, msg: str, color: str = None):
        self._status_var.set(msg)
        if color and self._status_label:
            try:
                self._status_label.config(fg=color)
            except Exception:
                pass

    def _flash_border(self):
        """Briefly flash the panel border red on a wrong password."""
        try:
            self._panel.config(highlightbackground=self.DANGER)
            self._root.after(
                450,
                lambda: self._panel.config(highlightbackground=self.PANEL_BORDER),
            )
        except Exception:
            pass

    def _play_sound(self, kind: str):
        if not Config.ENABLE_SOUND:
            return

        def _play():
            try:
                import winsound
                if kind == "error":
                    winsound.PlaySound(
                        "SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC
                    )
                elif kind == "success":
                    winsound.PlaySound(
                        "SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC
                    )
            except Exception:
                pass

        threading.Thread(target=_play, daemon=True).start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self):
        """Build and enter the tkinter event loop (blocking call)."""
        self._build()
        self._root.mainloop()

    def destroy(self):
        if self._root:
            try:
                self._root.quit()
                self._root.destroy()
            except Exception:
                pass
            self._root = None


# =============================================================================
#  WATCHDOG RECOVERY MODULE
# =============================================================================

class WatchdogRecoveryModule:
    """
    Background thread that monitors the integrity of all security components.
    Reapplies the keyboard hook and overlay topmost state if they are lost.
    """

    def __init__(
        self,
        keyboard_mod: KeyboardSecurityModule,
        overlay_ui: KioskOverlayUI,
        focus_enforcer: WindowFocusEnforcer,
    ):
        self._keyboard = keyboard_mod
        self._overlay  = overlay_ui
        self._enforcer = focus_enforcer
        self._running  = False
        self._thread   = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="WatchdogRecovery",
        )
        self._thread.start()
        log.info("Watchdog recovery module started OK")

    def stop(self):
        self._running = False

    def _watchdog_loop(self):
        while self._running:
            try:
                # Re-activate keyboard hook if lost
                if not self._keyboard.is_active():
                    log.warning("Watchdog: keyboard hook lost - reapplying!")
                    self._keyboard.start()

                # Only keep overlay on top when Chrome is NOT running
                # (when Chrome runs, the overlay should be hidden / withdrawn)
                chrome_alive = any(
                    proc.info.get("name", "").lower() == Config.TARGET_APP_NAME.lower()
                    for proc in psutil.process_iter(["name"])
                )
                if not chrome_alive and self._overlay and self._overlay._root:
                    try:
                        if self._overlay._root.state() != "withdrawn":
                            self._overlay._root.attributes("-topmost", True)
                            self._overlay._root.lift()
                    except Exception:
                        pass

            except Exception as e:
                log.error(f"Watchdog loop error: {e}")

            time.sleep(1.5)


# =============================================================================
#  CRASH RECOVERY MANAGER
# =============================================================================

class CrashRecoveryManager:
    """
    Registers atexit and signal handlers.

    CRITICAL: This class guarantees that the keyboard and Win key are always
    restored, even if the application crashes unexpectedly.
    """

    def __init__(self):
        self._keyboard_mod    = None
        self._focus_enforcer  = None
        self._process_monitor = None
        self._watchdog        = None
        self._overlay         = None
        self._winkey_mgr      = None
        self._registered      = False

    def register(
        self,
        keyboard_mod: KeyboardSecurityModule,
        focus_enforcer: WindowFocusEnforcer,
        process_monitor: ProcessMonitorModule,
        watchdog: WatchdogRecoveryModule,
        overlay: KioskOverlayUI,
        winkey_mgr: WinKeyManager,
    ):
        self._keyboard_mod    = keyboard_mod
        self._focus_enforcer  = focus_enforcer
        self._process_monitor = process_monitor
        self._watchdog        = watchdog
        self._overlay         = overlay
        self._winkey_mgr      = winkey_mgr

        if not self._registered:
            atexit.register(self.cleanup)
            signal.signal(signal.SIGINT,  self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
            self._registered = True
            log.info("Crash recovery handlers registered OK")

    def _signal_handler(self, signum, frame):
        log.info(f"Signal {signum} received - initiating cleanup.")
        self.cleanup()
        sys.exit(0)

    def cleanup(self):
        """
        Orderly shutdown routine.
        This method ALWAYS runs on exit — normal or abnormal.
        """
        log.info("-" * 50)
        log.info("CLEANUP INITIATED")
        log.info("-" * 50)

        # Stop background threads first
        for label, obj in [
            ("Watchdog",       self._watchdog),
            ("ProcessMonitor", self._process_monitor),
            ("FocusEnforcer",  self._focus_enforcer),
        ]:
            try:
                if obj:
                    obj.stop()
                    log.info(f"  {label} stopped.")
            except Exception as e:
                log.error(f"  {label} stop error: {e}")

        # CRITICAL: Restore keyboard
        try:
            if self._keyboard_mod:
                self._keyboard_mod.stop()
                log.info("  Keyboard restored OK")
        except Exception as e:
            log.error(f"  Keyboard restore error: {e}")
            try:
                keyboard.unhook_all()   # emergency fallback
            except Exception:
                pass

        # CRITICAL: Restore Win key registry
        try:
            if self._winkey_mgr:
                self._winkey_mgr.restore()
                log.info("  Win key registry restored OK")
        except Exception as e:
            log.error(f"  Win key restore error: {e}")

        # Restore Task Manager
        try:
            AdvancedSecurityModule.restore_task_manager()
        except Exception:
            pass

        # Destroy overlay
        try:
            if self._overlay:
                self._overlay.destroy()
        except Exception:
            pass

        log.info("-" * 50)
        log.info("CLEANUP COMPLETE - system fully restored")
        log.info("-" * 50)


# =============================================================================
#  MAIN APPLICATION CONTROLLER
# =============================================================================

class KioskLockerApp:
    """
    Top-level orchestrator.
    Initialises all modules in the correct sequence and manages lifecycle.
    """

    def __init__(self):
        self._crash_mgr      = CrashRecoveryManager()
        self._auth           = PasswordAuth()
        self._keyboard_mod   = KeyboardSecurityModule()
        self._winkey_mgr     = WinKeyManager()
        self._launcher       = AppLauncherModule()
        self._focus_enforcer = WindowFocusEnforcer()
        self._overlay        = None
        self._process_mon    = None
        self._watchdog       = None

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_chrome_crash(self):
        """Called by ProcessMonitor when Chrome process disappears."""
        if not Config.AUTO_RESTART_CHROME:
            return
        log.warning("Chrome crash/exit detected - showing overlay and relaunching in 2 s.")

        # Show the kiosk overlay while Chrome is absent
        if self._overlay and self._overlay._root:
            try:
                self._overlay._root.after(0, self._overlay._root.deiconify)
                self._overlay._root.after(0, self._overlay._root.lift)
                self._overlay._root.after(
                    0, lambda: self._overlay._root.attributes("-topmost", True)
                )
            except Exception:
                pass

        time.sleep(2)
        self._launcher.launch()

        # Wait up to 10 s for Chrome to be detectable
        for _ in range(20):
            if self._launcher.is_running():
                log.info("Chrome restarted OK")
                break
            time.sleep(0.5)
        else:
            log.warning("Chrome did not restart within timeout.")

        # Give Chrome another moment to fully paint before restarting focus enforcer
        time.sleep(2)
        self._focus_enforcer.stop()
        time.sleep(0.3)
        self._focus_enforcer.start()

    def _on_unlock(self):
        """
        Called when the correct PIN is entered on the overlay
        OR when the teacher presses U × 5.
        Stops all keyboard locks, restores system, lets Chrome keep running.
        """
        log.info("=== END-OF-EXAM UNLOCK TRIGGERED ===")
        self._shutdown_security()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown_security(self):
        """Stop all security modules. Chrome continues to run normally."""
        if self._watchdog:
            self._watchdog.stop()
        if self._process_mon:
            self._process_mon.stop()
        if self._focus_enforcer:
            self._focus_enforcer.stop()

        # Restore keyboard — students / teacher can now type freely
        self._keyboard_mod.stop()

        # Restore Win key registry to its original state
        self._winkey_mgr.restore()

        # Restore optional registry lockdowns
        AdvancedSecurityModule.restore_task_manager()

        # Close the overlay — Chrome is already on screen
        if self._overlay:
            self._overlay.destroy()

        log.info(
            "Kiosk security DEACTIVATED — keyboard unlocked, Chrome stays open."
        )

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def run(self):
        log.info("=" * 60)
        log.info("  ENTERPRISE KIOSK SECURITY SYSTEM v3.0 - STARTING")
        log.info(f"  Windows {platform.version()}  |  Python {sys.version.split()[0]}")
        log.info("=" * 60)

        # Step 1 - Require Administrator
        AdminPrivilegeManager.enforce_admin()

        # Step 2 - Registry lockdowns
        log.info("Applying registry security...")
        self._winkey_mgr.disable()
        AdvancedSecurityModule.disable_task_manager()

        # Step 3 - Kill stale Chrome sessions, launch fresh
        log.info("Launching Chrome in kiosk mode...")
        self._launcher.kill_existing_chrome()
        launched = self._launcher.launch()
        if not launched:
            log.error(
                "Chrome could not be launched. "
                "Check Config.CHROME_PATHS or install Chrome."
            )

        # Step 4 - Wait for Chrome process to appear
        log.info("Waiting for Chrome to start...")
        for _ in range(40):
            if self._launcher.is_running():
                log.info("Chrome is running OK")
                break
            time.sleep(0.5)
        else:
            log.warning("Chrome did not start within timeout - continuing anyway.")

        # Step 5 - Activate global keyboard lock
        #          Register the U×5 secret unlock callback BEFORE starting
        self._keyboard_mod.set_unlock_callback(self._on_unlock)
        self._keyboard_mod.start()

        # Step 6 - Start window focus enforcement
        self._focus_enforcer.start()

        # Step 7 - Start Chrome process monitor
        self._process_mon = ProcessMonitorModule(
            self._launcher,
            self._on_chrome_crash,
        )
        self._process_mon.start()

        # Step 8 - Build the overlay UI
        self._overlay = KioskOverlayUI(
            auth=self._auth,
            keyboard_module=self._keyboard_mod,
            on_unlock=self._on_unlock,
        )

        # Step 9 - Start watchdog recovery
        self._watchdog = WatchdogRecoveryModule(
            keyboard_mod=self._keyboard_mod,
            overlay_ui=self._overlay,
            focus_enforcer=self._focus_enforcer,
        )
        self._watchdog.start()

        # Step 10 - Register crash/signal cleanup handlers
        self._crash_mgr.register(
            keyboard_mod=self._keyboard_mod,
            focus_enforcer=self._focus_enforcer,
            process_monitor=self._process_mon,
            watchdog=self._watchdog,
            overlay=self._overlay,
            winkey_mgr=self._winkey_mgr,
        )

        log.info("All security modules active.")
        log.info(f"Password hash prefix: {Config.ADMIN_PASSWORD_HASH[:16]}...")
        log.info("Entering kiosk overlay loop...")

        # Step 11 - Run the overlay (blocking mainloop)
        try:
            self._overlay.run()
        except Exception as e:
            log.error(f"Overlay exception: {e}\n{traceback.format_exc()}")
        finally:
            self._crash_mgr.cleanup()


# =============================================================================
#  ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    try:
        app = KioskLockerApp()
        app.run()
    except SystemExit:
        pass
    except Exception:
        log.critical(f"FATAL UNHANDLED EXCEPTION:\n{traceback.format_exc()}")

        # Emergency keyboard restore
        try:
            keyboard.unhook_all()
        except Exception:
            pass

        # Emergency Win key registry restore
        try:
            _emgr = WinKeyManager()
            _emgr._is_disabled = True          # pretend it was disabled
            _emgr._original_existed = False    # so we delete the entry
            _emgr.restore()
        except Exception:
            pass

        ctypes.windll.user32.MessageBoxW(
            0,
            "A fatal error occurred.\n\n"
            "Keyboard and Windows key have been restored.\n\n"
            "See kiosk_locker.log for full details.",
            "Kiosk Locker - Fatal Error",
            0x10,
        )
        sys.exit(1)
