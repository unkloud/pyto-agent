"""iOS capability adapters, each degrading gracefully off-device.

Every function returns a :class:`CapabilityResult` and *never* raises for a missing
capability.  That is the whole design: the harness runs on a phone, in a terminal on a
laptop and under ``unittest`` on Linux, and the caller must not need to know which.

Two rules keep this honest:

1. **Nothing iOS-specific is imported at module import time.**  Imports happen inside
   ``_optional_module`` / ``_optional_attr`` and are recorded, so importing this module
   on Linux is a no-op rather than an ImportError.
2. **A fallback is reported as a fallback.**  When the share sheet is unavailable the
   text is written to a file and the result says ``supported=False`` with the path; it
   never claims the user saw a share sheet.

``url_scheme`` values are exact and worth knowing by heart:

* Shortcuts: ``shortcuts://run-shortcut?name=<urlencoded>&input=text&text=<urlencoded>``
* Files app: ``shareddocuments://<path>`` (and ``shareddocuments://`` for the root)
* Pyto script: ``pyto://python/<urlencoded path>``
* This harness: ``pyto://python/<path to run.py>`` with a ``?`` query the script reads
  out of ``sys.argv`` — see README.md for the Shortcuts wiring.
"""

from __future__ import annotations

import importlib
import os
import platform
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import ConfigError
from .home import expand_user_path

#: Optional modules probed on iOS.  Kept as data so tests can assert the probe list.
PYTO_MODULES = (
    "pyto",
    "pyto_ui",
    "pasteboard",
    "file_system",
    "notifications",
    "speech",
    "photos",
    "calendar_events",
    "background",
    "xcallback",
    "usernotification",
)

SHORTCUTS_SCHEME = "shortcuts://run-shortcut"
FILES_SCHEME = "shareddocuments://"
PYTO_SCHEME = "pyto://python/"


@dataclass
class CapabilityResult:
    """Outcome of one capability attempt.

    ``supported`` answers "does this device/interpreter have this capability at all",
    ``ok`` answers "did the call work".  A share that fell back to writing a file has
    ``supported=False, ok=True`` — the model needs both facts to explain itself to the
    user.
    """

    action: str
    ok: bool = False
    supported: bool = False
    detail: str = ""
    method: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "action": self.action,
            "ok": self.ok,
            "supported": self.supported,
            "method": self.method,
            "detail": self.detail,
        }
        if self.data:
            payload["data"] = dict(self.data)
        payload["elapsed_ms"] = round(self.elapsed_ms, 3)
        return payload

    def render(self) -> str:
        """One-line, model-facing rendering."""
        status = "ok" if self.ok else "failed"
        support = "native" if self.supported else "unsupported"
        parts = ["{}: {} ({})".format(self.action, status, support)]
        if self.method:
            parts.append("via {}".format(self.method))
        if self.detail:
            parts.append(self.detail)
        if self.data:
            rendered = ", ".join("{}={!r}".format(k, v) for k, v in sorted(self.data.items()))
            parts.append("[" + rendered + "]")
        return " | ".join(parts)


#: Append-only record of every capability attempt, for diagnostics and tests.
RECORD: List[CapabilityResult] = []
_RECORD_LIMIT = 200


def record(result: CapabilityResult) -> CapabilityResult:
    RECORD.append(result)
    if len(RECORD) > _RECORD_LIMIT:
        del RECORD[: len(RECORD) - _RECORD_LIMIT]
    return result


def clear_record() -> None:
    RECORD.clear()


def is_ios() -> bool:
    """True on an iOS CPython build, i.e. inside Pyto."""
    return sys.platform == "ios" or "ios" in platform.platform().lower()


def is_pyto() -> bool:
    """True when Pyto's runtime modules are present (installed *and* importable).

    ``find_spec`` is used rather than a plain import so that probing does not execute a
    bridge module as a side effect.
    """
    if is_ios():
        return True
    for name in ("pyto", "_ios_popen"):
        try:
            if importlib.util.find_spec(name) is not None:
                return True
        except (ImportError, ValueError, AttributeError):
            continue
    return False


def has_fake_subprocess() -> bool:
    """True when ``subprocess`` cannot really fork or kill — which is Pyto's case.

    In Pyto, ``subprocess.Popen`` runs the child *in process and synchronously*;
    ``kill()``/``terminate()`` are empty no-ops, ``os.fork`` is a stub and ``os.waitpid``
    returns ``(-1, 0)``.  A timeout implemented by killing a "child" therefore never kills
    anything, which is why the harness runs programs in-process on device instead.

    A single process-group kill is actually attempted once, because spawning a process is
    the only honest way to tell a real fork from a stub.  The verdict is cached.
    """
    global _REAL_SUBPROCESS
    if is_pyto():
        return True
    if _REAL_SUBPROCESS is None:
        _REAL_SUBPROCESS = _probe_real_subprocess()
    return not _REAL_SUBPROCESS


#: Cached answer from :func:`_probe_real_subprocess`; ``None`` until probed.
_REAL_SUBPROCESS: Optional[bool] = None


def _probe_real_subprocess() -> bool:
    """Spawn a sleeping process, verify a process-group kill works, return the verdict."""
    import signal as _signal
    import subprocess as _subprocess

    executable = sys.executable
    if not executable or not os.path.exists(executable):
        return False
    if not (hasattr(os, "killpg") and hasattr(os, "getpgid")):
        return False
    try:
        process = _subprocess.Popen(
            [executable, "-c", "import time; time.sleep(5)"],
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return False
    try:
        try:
            os.killpg(os.getpgid(process.pid), _signal.SIGTERM)
        except (OSError, AttributeError):
            return False
        try:
            process.wait(timeout=3)
        except _subprocess.TimeoutExpired:
            return False
        return True
    finally:
        try:
            process.kill()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


def platform_label() -> str:
    return "{} / {} {}".format(
        "iOS" if is_ios() else "desktop",
        platform.python_implementation(),
        platform.python_version(),
    )


# --------------------------------------------------------------------------------------
# Optional-module plumbing
# --------------------------------------------------------------------------------------


def _optional_module(name: str) -> Any:
    """Import an optional module, returning ``None`` when it is absent.

    ``ImportError`` and ``AttributeError`` are the expected failures.  A module that is
    present but broken at import time (which happens with bridge modules) is recorded
    as a miss rather than crashing the harness.
    """
    try:
        return importlib.import_module(name)
    except BaseException:  # noqa: BLE001 - a bridge module may raise anything on import
        return None


def _optional_attr(module_name: str, attr: str) -> Any:
    module = _optional_module(module_name)
    if module is None:
        return None
    return getattr(module, attr, None)


def _framework_class(framework: str, class_name: str) -> Any:
    """Look up an Objective-C class exposed either as a module or via the bridge.

    Pyto exposes some frameworks as importable modules (``import AVFoundation``) and
    others only through the Objective-C bridge, so both are tried before giving up.
    """
    direct = _optional_attr(framework, class_name)
    if direct is not None:
        return direct
    for bridge_name in ("objc", "rubicon.objc", "pyto_ui"):
        module = _optional_module(bridge_name)
        if module is None:
            continue
        getter = getattr(module, "ObjCClass", None)
        if callable(getter):
            try:
                return getter(class_name, framework)
            except Exception:  # noqa: BLE001 - class not present in this runtime
                continue
    return None


def available_capabilities() -> Dict[str, bool]:
    """Probe table: which native hooks exist on this interpreter."""
    file_system = _optional_module("file_system")
    return {
        "pasteboard": _optional_module("pasteboard") is not None,
        "share": bool(file_system and callable(getattr(file_system, "share_text", None))),
        "notifications": _optional_module("notifications") is not None,
        "usernotification": _optional_module("usernotification") is not None,
        "speech": _optional_module("speech") is not None,
        "photos": _optional_module("photos") is not None,
        "calendar_events": _optional_module("calendar_events") is not None,
        "background": _optional_module("background") is not None,
        "xcallback": _optional_module("xcallback") is not None,
        "pyto_ui": _optional_module("pyto_ui") is not None,
        "webbrowser": _browser_available(),
    }


def _browser_available() -> bool:
    try:
        webbrowser.get()
        return True
    except Exception:  # noqa: BLE001 - webbrowser.Error, or a platform quirk
        return False


# --------------------------------------------------------------------------------------
# Clipboard
# --------------------------------------------------------------------------------------


def clipboard_get() -> CapabilityResult:
    """Read the system clipboard.  iOS shows a paste banner; that is expected."""
    started = time.monotonic()
    module = _optional_module("pasteboard")
    if module is not None and hasattr(module, "get"):
        try:
            value = module.get()
            text = value if isinstance(value, str) else ("" if value is None else str(value))
            return record(
                CapabilityResult(
                    action="clipboard_get",
                    ok=True,
                    supported=True,
                    method="pasteboard.get",
                    detail="read {} characters".format(len(text)),
                    data={"text": text, "chars": len(text)},
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the bridge can fail on a locked device
            return record(
                CapabilityResult(
                    action="clipboard_get",
                    ok=False,
                    supported=True,
                    method="pasteboard.get",
                    detail="{}: {}".format(type(exc).__name__, exc),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
    return record(
        CapabilityResult(
            action="clipboard_get",
            ok=False,
            supported=False,
            method="none",
            detail="no clipboard bridge on this interpreter (Pyto provides `pasteboard`)",
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


def clipboard_set(text: str) -> CapabilityResult:
    """Write the system clipboard."""
    started = time.monotonic()
    module = _optional_module("pasteboard")
    if module is not None and hasattr(module, "set"):
        try:
            module.set(str(text))
            return record(
                CapabilityResult(
                    action="clipboard_set",
                    ok=True,
                    supported=True,
                    method="pasteboard.set",
                    detail="copied {} characters".format(len(text)),
                    data={"chars": len(text)},
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return record(
                CapabilityResult(
                    action="clipboard_set",
                    ok=False,
                    supported=True,
                    method="pasteboard.set",
                    detail="{}: {}".format(type(exc).__name__, exc),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
    return record(
        CapabilityResult(
            action="clipboard_set",
            ok=False,
            supported=False,
            method="none",
            detail="no clipboard bridge on this interpreter; the text is unchanged: {!r}".format(text[:80]),
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


# --------------------------------------------------------------------------------------
# Share sheet
# --------------------------------------------------------------------------------------


def share_text(text: str, *, title: str = "") -> CapabilityResult:
    """Open the iOS share sheet with ``text``.  Falls back to writing a file."""
    started = time.monotonic()
    module = _optional_module("file_system")
    share_text_fn = getattr(module, "share_text", None) if module is not None else None
    if callable(share_text_fn):
        try:
            share_text_fn(str(text))
            return record(
                CapabilityResult(
                    action="share_text",
                    ok=True,
                    supported=True,
                    method="file_system.share_text",
                    detail="share sheet opened for {} characters{}".format(
                        len(text), " ({})".format(title) if title else ""
                    ),
                    data={"chars": len(text)},
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return record(
                CapabilityResult(
                    action="share_text",
                    ok=False,
                    supported=True,
                    method="file_system.share_text",
                    detail="{}: {}".format(type(exc).__name__, exc),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
    # Fallback: persist the text so the user can still get at it.
    fallback = write_fallback_file(text, prefix=title or "share", suffix=".txt")
    return record(
        CapabilityResult(
            action="share_text",
            ok=fallback is not None,
            supported=False,
            method="file-fallback",
            detail=(
                "no share sheet on this interpreter; text written to {}".format(fallback)
                if fallback
                else "no share sheet on this interpreter and the fallback file could not be written"
            ),
            data={"path": fallback} if fallback else {},
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


def share_file(path: str, *, text: str = "") -> CapabilityResult:
    """Open the share sheet for a file.  Falls back to reporting the path."""
    started = time.monotonic()
    if not os.path.exists(path):
        return record(
            CapabilityResult(
                action="share_file",
                ok=False,
                supported=False,
                method="none",
                detail="no such file: {}".format(path),
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    module = _optional_module("file_system")
    share_files_fn = getattr(module, "share_files", None) if module is not None else None
    if callable(share_files_fn):
        try:
            share_files_fn(os.path.abspath(path))
            return record(
                CapabilityResult(
                    action="share_file",
                    ok=True,
                    supported=True,
                    method="file_system.share_files",
                    detail="share sheet opened for {}".format(path),
                    data={"path": path},
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return record(
                CapabilityResult(
                    action="share_file",
                    ok=False,
                    supported=True,
                    method="file_system.share_files",
                    detail="{}: {}".format(type(exc).__name__, exc),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
    return record(
        CapabilityResult(
            action="share_file",
            ok=True,
            supported=False,
            method="file-fallback",
            detail="no share sheet on this interpreter; the file is at {}".format(os.path.abspath(path)),
            data={"path": os.path.abspath(path)},
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


def write_fallback_file(text: str, *, prefix: str = "share", suffix: str = ".txt", directory: str = "") -> Optional[str]:
    """Write ``text`` somewhere the user can reach it.  Returns the path or ``None``."""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in prefix)[:40].strip("-") or "share"
    target_dir = directory or os.path.join(tempfile.gettempdir(), "pyto-harness")
    try:
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, "{}-{}{}".format(safe, int(time.time()), suffix))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path
    except OSError:  # pragma: no cover - filesystem dependent
        return None


# --------------------------------------------------------------------------------------
# URL opening
# --------------------------------------------------------------------------------------


def open_url(url: str) -> CapabilityResult:
    """Open a URL, preferring the OS handler and falling back to ``webbrowser``."""
    started = time.monotonic()
    if not url:
        return record(
            CapabilityResult(
                action="open_url",
                ok=False,
                supported=False,
                method="none",
                detail="empty url",
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    # 1. Pyto's own opener, when present: it routes schemes through UIApplication.
    pyto_open = _optional_attr("pyto", "open_url")
    if callable(pyto_open):
        try:
            pyto_open(url)
            return record(
                CapabilityResult(
                    action="open_url",
                    ok=True,
                    supported=True,
                    method="pyto.open_url",
                    detail="opened {}".format(url),
                    data={"url": url},
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
        except Exception as exc:  # noqa: BLE001 - fall through to webbrowser
            failure = "pyto.open_url failed: {}: {}".format(type(exc).__name__, exc)
    else:
        failure = ""

    # 2. webbrowser: real on iOS (it hands the scheme to the system) and on a desktop with
    #    a browser.  The environment flag exists for the test suite and headless hosts,
    #    where spawning a browser is either impossible or rude.
    if os.environ.get("PYTO_HARNESS_NO_BROWSER"):
        return record(
            CapabilityResult(
                action="open_url",
                ok=False,
                supported=False,
                method="suppressed",
                detail="url opening is disabled by PYTO_HARNESS_NO_BROWSER; url was {}".format(url),
                data={"url": url},
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    try:
        opened = webbrowser.open(url)
        return record(
            CapabilityResult(
                action="open_url",
                ok=bool(opened),
                supported=True,
                method="webbrowser",
                detail=("opened {}".format(url) if opened else "the platform refused to open {}".format(url)),
                data={"url": url},
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    except Exception as exc:  # noqa: BLE001 - webbrowser.Error on a headless box
        detail = "{}: {}".format(type(exc).__name__, exc)
        if failure:
            detail = failure + "; webbrowser: " + detail
        return record(
            CapabilityResult(
                action="open_url",
                ok=False,
                supported=False,
                method="none",
                detail="could not open the URL ({})".format(detail),
                data={"url": url},
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )


# --------------------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------------------


def notify(title: str, body: str = "", *, sound: bool = True) -> CapabilityResult:
    """Post a local notification.  Requires notification permission on iOS."""
    started = time.monotonic()
    module = _optional_module("notifications")
    if module is not None:
        for attempt in (
            lambda: module.send(title, body),
            lambda: module.schedule(title, body, delay=1),
            lambda: module.notify(title, body),
        ):
            try:
                attempt()
                return record(
                    CapabilityResult(
                        action="notify",
                        ok=True,
                        supported=True,
                        method="notifications",
                        detail="notification posted: {!r}".format(title),
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                )
            except AttributeError:
                continue
            except Exception as exc:  # noqa: BLE001
                return record(
                    CapabilityResult(
                        action="notify",
                        ok=False,
                        supported=True,
                        method="notifications",
                        detail="{}: {}".format(type(exc).__name__, exc),
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                )
    # Objective-C bridge fallback.
    center = _optional_attr("usernotification", "UNUserNotificationCenter")
    if center is not None:
        try:
            request = _optional_attr("usernotification", "UNMutableNotificationContent")
            trigger_cls = _optional_attr("usernotification", "UNTimeIntervalNotificationTrigger")
            content = request.new() if hasattr(request, "new") else request()
            content.setTitle_(title)
            content.setBody_(body)
            if trigger_cls is not None:
                trigger = trigger_cls.triggerWithTimeInterval_repeats_(1, False)
                notification = _optional_attr("usernotification", "UNNotificationRequest")
                req = notification.requestWithIdentifier_content_trigger_(
                    "pyto-harness-{}".format(int(time.time())), content, trigger
                )
                center.currentNotificationCenter().addNotificationRequest_withCompletionHandler_(req, None)
            return record(
                CapabilityResult(
                    action="notify",
                    ok=True,
                    supported=True,
                    method="usernotification",
                    detail="notification posted: {!r}".format(title),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return record(
                CapabilityResult(
                    action="notify",
                    ok=False,
                    supported=True,
                    method="usernotification",
                    detail="{}: {}".format(type(exc).__name__, exc),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
    return record(
        CapabilityResult(
            action="notify",
            ok=False,
            supported=False,
            method="none",
            detail="no notifications bridge on this interpreter; print this instead: {} - {}".format(title, body),
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


# --------------------------------------------------------------------------------------
# Speech
# --------------------------------------------------------------------------------------


def speak(text: str, *, language: str = "en-US", rate: float = 0.5) -> CapabilityResult:
    """Speak ``text`` aloud.  On iOS the audio session usually needs the app foregrounded."""
    started = time.monotonic()
    module = _optional_module("speech")
    if module is not None:
        for name in ("say", "speak"):
            talker = getattr(module, name, None)
            if callable(talker):
                try:
                    talker(str(text))
                    return record(
                        CapabilityResult(
                            action="speak",
                            ok=True,
                            supported=True,
                            method="speech.{}".format(name),
                            detail="speaking {} characters".format(len(text)),
                            elapsed_ms=(time.monotonic() - started) * 1000,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    return record(
                        CapabilityResult(
                            action="speak",
                            ok=False,
                            supported=True,
                            method="speech.{}".format(name),
                            detail="{}: {}".format(type(exc).__name__, exc),
                            elapsed_ms=(time.monotonic() - started) * 1000,
                        )
                    )
    synthesizer_cls = _framework_class("AVFoundation", "AVSpeechSynthesizer")
    utterance_cls = _framework_class("AVFoundation", "AVSpeechUtterance")
    if synthesizer_cls is not None and utterance_cls is not None:
        try:
            utterance = utterance_cls.speechUtteranceWithString_(str(text))
            utterance.setRate_(rate)
            voice_cls = _framework_class("AVFoundation", "AVSpeechSynthesisVoice")
            if voice_cls is not None:
                voice = voice_cls.voiceWithLanguage_(language)
                if voice is not None:
                    utterance.setVoice_(voice)
            synthesizer_cls.new().speakUtterance_(utterance)
            return record(
                CapabilityResult(
                    action="speak",
                    ok=True,
                    supported=True,
                    method="AVSpeechSynthesizer",
                    detail="speaking {} characters".format(len(text)),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return record(
                CapabilityResult(
                    action="speak",
                    ok=False,
                    supported=True,
                    method="AVSpeechSynthesizer",
                    detail="{}: {}".format(type(exc).__name__, exc),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
    return record(
        CapabilityResult(
            action="speak",
            ok=False,
            supported=False,
            method="none",
            detail="no speech bridge on this interpreter; the text is printed instead: {!r}".format(text[:120]),
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


# --------------------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------------------


def save_photo(path: str) -> CapabilityResult:
    """Save an image file to the iOS photo library.

    Needs photo-library *add* permission.  On anything but iOS this reports
    ``supported=False`` — it does not pretend the picture was saved.
    """
    started = time.monotonic()
    try:
        absolute = expand_user_path(path, what="image path")
    except ConfigError as exc:
        # A '~' this device cannot expand must not become a path literally named '~'.
        return record(
            CapabilityResult(
                action="save_photo",
                ok=False,
                supported=False,
                method="none",
                detail=str(exc).splitlines()[0],
                data={"path": str(path)},
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    if not os.path.exists(absolute):
        return record(
            CapabilityResult(
                action="save_photo",
                ok=False,
                supported=False,
                method="none",
                detail="no such image file: {}".format(absolute),
                data={"path": absolute},
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    extension = os.path.splitext(absolute)[1].lower()
    if extension not in (".png", ".jpg", ".jpeg", ".heic", ".gif", ".tiff", ".bmp", ".webp"):
        return record(
            CapabilityResult(
                action="save_photo",
                ok=False,
                supported=True,
                method="none",
                detail="{} is not an image extension the photo library accepts".format(extension or "<none>"),
                data={"path": absolute},
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    module = _optional_module("photos")
    if module is not None:
        for name in ("save_image", "save", "add_image"):
            saver = getattr(module, name, None)
            if callable(saver):
                try:
                    result = saver(absolute)
                    if hasattr(result, "__await__"):  # a bridge may hand back an awaitable
                        detail = "save dispatched asynchronously"
                    else:
                        detail = "saved {} to the photo library".format(os.path.basename(absolute))
                    return record(
                        CapabilityResult(
                            action="save_photo",
                            ok=True,
                            supported=True,
                            method="photos.{}".format(name),
                            detail=detail,
                            data={"path": absolute},
                            elapsed_ms=(time.monotonic() - started) * 1000,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - almost always a permission denial
                    return record(
                        CapabilityResult(
                            action="save_photo",
                            ok=False,
                            supported=True,
                            method="photos.{}".format(name),
                            detail="{}: {} (photo-library permission is required)".format(
                                type(exc).__name__, exc
                            ),
                            data={"path": absolute},
                            elapsed_ms=(time.monotonic() - started) * 1000,
                        )
                    )
    return record(
        CapabilityResult(
            action="save_photo",
            ok=False,
            supported=False,
            method="none",
            detail=(
                "no photo-library bridge on this interpreter; the image stays at {}".format(absolute)
            ),
            data={"path": absolute},
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


# --------------------------------------------------------------------------------------
# Shortcuts and the Files app
# --------------------------------------------------------------------------------------


def shortcut_url(name: str, input_text: str = "", *, mode: str = "run-shortcut") -> str:
    """Build the x-callback URL that runs a Shortcut by name.  Pure function, testable."""
    query = [("name", name)]
    if input_text:
        # Current iOS accepts the documented `input=text&text=<value>` pair.
        query.append(("input", "text"))
        query.append(("text", input_text))
    return "shortcuts://{}?{}".format(mode, urllib.parse.urlencode(query))


def shortcut_run(name: str, input_text: str = "") -> CapabilityResult:
    """Run a Shortcut by name via its URL scheme.  Returns the callback URL used."""
    started = time.monotonic()
    name = (name or "").strip()
    if not name:
        return record(
            CapabilityResult(
                action="shortcut_run",
                ok=False,
                supported=False,
                method="none",
                detail="a Shortcut name is required",
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    url = shortcut_url(name, input_text)
    opened = open_url(url)
    result = CapabilityResult(
        action="shortcut_run",
        ok=opened.ok,
        supported=opened.supported,
        method="url-scheme",
        detail=(
            "asked iOS to run the Shortcut {!r} via {}".format(name, url)
            if opened.ok
            else "could not hand {!r} to iOS ({})".format(url, opened.detail)
        ),
        data={"name": name, "url": url, "url_opened": opened.ok},
        elapsed_ms=(time.monotonic() - started) * 1000,
    )
    return record(result)


def open_in_files(path: str = "") -> CapabilityResult:
    """Reveal a path (or the Files app root) via ``shareddocuments://``."""
    started = time.monotonic()
    if path:
        try:
            absolute = expand_user_path(path, what="path")
        except ConfigError as exc:
            return record(
                CapabilityResult(
                    action="open_in_files",
                    ok=False,
                    supported=False,
                    method="none",
                    detail=str(exc).splitlines()[0],
                    data={"path": str(path)},
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
        url = FILES_SCHEME + urllib.parse.quote(absolute)
        targets = [url, FILES_SCHEME]
    else:
        absolute = ""
        url = FILES_SCHEME
        targets = [FILES_SCHEME]
    last = ""
    for candidate in targets:
        opened = open_url(candidate)
        if opened.ok:
            return record(
                CapabilityResult(
                    action="open_in_files",
                    ok=True,
                    supported=opened.supported,
                    method="url-scheme",
                    detail="asked the Files app to show {}".format(absolute or "its root"),
                    data={"url": candidate, "path": absolute},
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
        last = opened.detail
    return record(
        CapabilityResult(
            action="open_in_files",
            ok=False,
            supported=False,
            method="url-scheme",
            detail="could not open the Files app ({})".format(last),
            data={"url": url, "path": absolute},
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


def pyto_run_url(script_path: str, arguments: Optional[Mapping[str, str]] = None) -> str:
    """URL that makes Pyto run a script — the anchor for the Shortcuts wiring."""
    query = dict(arguments or {})
    url = PYTO_SCHEME + urllib.parse.quote(expand_user_path(script_path, what="script path"))
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def capability_report() -> str:
    """Human summary used by ``run.py --capabilities`` and the system prompt."""
    caps = available_capabilities()
    native = sorted(name for name, present in caps.items() if present)
    missing = sorted(name for name, present in caps.items() if not present)
    lines = ["platform: {}".format(platform_label())]
    lines.append("native: {}".format(", ".join(native) or "<none>"))
    lines.append("unavailable (will degrade): {}".format(", ".join(missing) or "<none>"))
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Calendar (Pyto's `calendar_events`)
# --------------------------------------------------------------------------------------


def _iso_to_epoch(value: str) -> Optional[float]:
    """Parse an ISO-8601-ish local timestamp.  Returns ``None`` when it is not one."""
    text = (value or "").strip()
    if not text:
        return None
    import datetime

    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def calendar_add_event(
    title: str,
    start_iso: str,
    end_iso: str = "",
    *,
    notes: str = "",
    calendar: str = "",
    all_day: bool = False,
) -> CapabilityResult:
    """Save an event with Pyto's undocumented ``calendar_events`` module.

    The module exposes ``save_event(...)``; its exact signature varies between builds, so
    two call shapes are attempted and the failure is reported rather than guessed at.
    iOS asks for calendar permission the first time.
    """
    started = time.monotonic()
    epoch = _iso_to_epoch(start_iso)
    if epoch is None:
        return record(
            CapabilityResult(
                action="calendar_add_event",
                ok=False,
                supported=True,
                method="none",
                detail="start_iso must be an ISO timestamp like 2024-05-01T09:00, got {!r}".format(start_iso),
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    module = _optional_module("calendar_events")
    saver = getattr(module, "save_event", None) if module is not None else None
    if callable(saver):
        end_epoch = _iso_to_epoch(end_iso) if end_iso else epoch + 3600
        attempts = (
            lambda: saver(title=title, start=epoch, end=end_epoch, notes=notes or None, calendar=calendar or None),
            lambda: saver(title, epoch, end_epoch, notes or None, calendar or None),
            lambda: saver(title=title, start=epoch, end=end_epoch),
            lambda: saver(title, epoch, end_epoch),
        )
        last: Optional[BaseException] = None
        for attempt in attempts:
            try:
                attempt()
                return record(
                    CapabilityResult(
                        action="calendar_add_event",
                        ok=True,
                        supported=True,
                        method="calendar_events.save_event",
                        detail="saved {!r} at {}".format(title, start_iso),
                        data={"title": title, "start_iso": start_iso, "end_iso": end_iso, "all_day": all_day},
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                )
            except TypeError as exc:  # signature mismatch: try the next shape
                last = exc
                continue
            except Exception as exc:  # noqa: BLE001 - almost always a permission denial
                return record(
                    CapabilityResult(
                        action="calendar_add_event",
                        ok=False,
                        supported=True,
                        method="calendar_events.save_event",
                        detail="{}: {} (calendar permission is required)".format(type(exc).__name__, exc),
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                )
        return record(
            CapabilityResult(
                action="calendar_add_event",
                ok=False,
                supported=True,
                method="calendar_events.save_event",
                detail="no known save_event signature matched this Pyto build ({})".format(last),
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    return record(
        CapabilityResult(
            action="calendar_add_event",
            ok=False,
            supported=False,
            method="none",
            detail=(
                "no calendar bridge on this interpreter; on iOS, Pyto provides "
                "`calendar_events`. Falling back to a Shortcut that adds the event is the usual fix."
            ),
            data={"title": title, "start_iso": start_iso, "end_iso": end_iso, "notes": notes},
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


def calendar_list_events(days: int = 7) -> CapabilityResult:
    """List upcoming events, when the bridge exposes a reader."""
    started = time.monotonic()
    module = _optional_module("calendar_events")
    if module is None:
        return record(
            CapabilityResult(
                action="calendar_list_events",
                ok=False,
                supported=False,
                method="none",
                detail="no calendar bridge on this interpreter (Pyto provides `calendar_events`)",
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    reader = None
    for name in ("get_events", "events", "fetch_events", "list_events"):
        candidate = getattr(module, name, None)
        if callable(candidate):
            reader = (name, candidate)
            break
    if reader is None:
        return record(
            CapabilityResult(
                action="calendar_list_events",
                ok=False,
                supported=False,
                method="calendar_events",
                detail=(
                    "this Pyto build's calendar_events module has no reader "
                    "(only writes); listing events needs a Shortcut"
                ),
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    name, function = reader
    try:
        try:
            events = function(days=days)
        except TypeError:
            events = function()
    except Exception as exc:  # noqa: BLE001
        return record(
            CapabilityResult(
                action="calendar_list_events",
                ok=False,
                supported=True,
                method="calendar_events.{}".format(name),
                detail="{}: {}".format(type(exc).__name__, exc),
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    rendered = []
    for event in events or ():
        if isinstance(event, Mapping):
            rendered.append(
                {
                    "title": event.get("title"),
                    "start": event.get("start") or event.get("start_date"),
                    "end": event.get("end") or event.get("end_date"),
                }
            )
        else:
            rendered.append({"repr": str(event)})
    return record(
        CapabilityResult(
            action="calendar_list_events",
            ok=True,
            supported=True,
            method="calendar_events.{}".format(name),
            detail="{} event(s) in the next {} day(s)".format(len(rendered), days),
            data={"events": rendered},
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


# --------------------------------------------------------------------------------------
# Background keepalive (Pyto's `background.BackgroundTask`)
# --------------------------------------------------------------------------------------


def keepalive_start(label: str = "pyto-harness", *, seconds: float = 0.0) -> CapabilityResult:
    """Keep the script alive while the app is backgrounded.

    Pyto's ``background.BackgroundTask`` plays silence for the lifetime of the task, which
    is the only mechanism available for surviving a background transition.  It is a
    guideline grey area (it is the same trick podcast apps use) and Apple review has
    rejected apps for abusing it, so it is opt-in and clearly labelled.
    """
    started = time.monotonic()
    module = _optional_module("background")
    task_cls = getattr(module, "BackgroundTask", None) if module is not None else None
    if task_cls is None:
        return record(
            CapabilityResult(
                action="keepalive_start",
                ok=False,
                supported=False,
                method="none",
                detail=(
                    "no background bridge on this interpreter; the app will be suspended when "
                    "the user leaves it (Pyto provides `background.BackgroundTask`)"
                ),
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    for attempt in (
        lambda: task_cls(id=label),
        lambda: task_cls(label),
        lambda: task_cls(),
    ):
        try:
            task = attempt()
        except TypeError:
            continue
        except Exception as exc:  # noqa: BLE001
            return record(
                CapabilityResult(
                    action="keepalive_start",
                    ok=False,
                    supported=True,
                    method="background.BackgroundTask",
                    detail="{}: {}".format(type(exc).__name__, exc),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            )
        starter = getattr(task, "start", None)
        if callable(starter):
            try:
                starter()
            except Exception as exc:  # noqa: BLE001
                return record(
                    CapabilityResult(
                        action="keepalive_start",
                        ok=False,
                        supported=True,
                        method="background.BackgroundTask.start",
                        detail="{}: {}".format(type(exc).__name__, exc),
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                )
        _KEEPALIVE["task"] = task
        _KEEPALIVE["label"] = label
        return record(
            CapabilityResult(
                action="keepalive_start",
                ok=True,
                supported=True,
                method="background.BackgroundTask",
                detail=(
                    "background task {!r} started; the app can keep running briefly after it is "
                    "backgrounded. This is a guideline grey area - do not leave it running.".format(label)
                ),
                data={"label": label, "seconds": seconds},
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    return record(
        CapabilityResult(
            action="keepalive_start",
            ok=False,
            supported=True,
            method="background.BackgroundTask",
            detail="no known BackgroundTask constructor signature matched this Pyto build",
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


#: The one live keepalive task, if any.
_KEEPALIVE: Dict[str, Any] = {}


def keepalive_stop() -> CapabilityResult:
    """End the background task started by :func:`keepalive_start`."""
    started = time.monotonic()
    task = _KEEPALIVE.get("task")
    if task is None:
        return record(
            CapabilityResult(
                action="keepalive_stop",
                ok=True,
                supported=False,
                method="none",
                detail="no background task is running",
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    stopped = False
    for name in ("stop", "end", "cancel"):
        function = getattr(task, name, None)
        if callable(function):
            try:
                function()
                stopped = True
                break
            except Exception as exc:  # noqa: BLE001
                return record(
                    CapabilityResult(
                        action="keepalive_stop",
                        ok=False,
                        supported=True,
                        method="background.BackgroundTask.{}".format(name),
                        detail="{}: {}".format(type(exc).__name__, exc),
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                )
    _KEEPALIVE.clear()
    return record(
        CapabilityResult(
            action="keepalive_stop",
            ok=stopped,
            supported=True,
            method="background.BackgroundTask",
            detail="background task stopped" if stopped else "the task object had no stop method",
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


# --------------------------------------------------------------------------------------
# Memory pressure
# --------------------------------------------------------------------------------------


def available_memory_bytes() -> Optional[int]:
    """Free memory in bytes, via Pyto's ``os_proc_available_memory`` when present."""
    for module_name, attr in (("os", "os_proc_available_memory"), ("pyto", "os_proc_available_memory")):
        function = _optional_attr(module_name, attr) if module_name != "os" else getattr(os, attr, None)
        if callable(function):
            try:
                return int(function())
            except Exception:  # noqa: BLE001 - the syscall can fail under pressure
                return None
    return None


def memory_status() -> CapabilityResult:
    """Report free memory.

    Pyto stops **every** running script once free memory reaches roughly 500 MB, so the
    harness checks this before large writes and caps session logs and tool output.
    """
    started = time.monotonic()
    available = available_memory_bytes()
    if available is None:
        return record(
            CapabilityResult(
                action="memory_status",
                ok=True,
                supported=False,
                method="none",
                detail=(
                    "free memory is not reported on this interpreter; on iOS it comes from "
                    "os_proc_available_memory(), and Pyto stops all scripts near 500 MB free"
                ),
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    megabytes = available / (1024 * 1024)
    pressure = "ok"
    if megabytes < 600:
        pressure = "critical: Pyto stops every script near 500 MB free"
    elif megabytes < 900:
        pressure = "tight: keep outputs small"
    return record(
        CapabilityResult(
            action="memory_status",
            ok=True,
            supported=True,
            method="os_proc_available_memory",
            detail="{:.0f} MB free ({})".format(megabytes, pressure),
            data={"available_bytes": available, "available_mb": round(megabytes, 1), "pressure": pressure},
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )


# --------------------------------------------------------------------------------------
# Shortcuts with a result (x-callback-url)
# --------------------------------------------------------------------------------------


XCALLBACK_SHORTCUTS = "shortcuts://x-callback-url/run-shortcut"


def shortcut_wait_url(name: str, input_text: str = "", *, callback: str = "pyto://") -> str:
    """x-callback-url form: iOS returns the Shortcut's output to ``callback``.

    The callback arrives with a ``result`` query parameter, e.g.
    ``pyto://?result=<output>``.  Nothing in the app can *wait* for it synchronously —
    the URL round trip happens through the app delegate — so the honest implementation
    records the URL and tells the model how the result comes back.
    """
    query = [("name", name)]
    if input_text:
        query.append(("input", "text"))
        query.append(("text", input_text))
    query.append(("x-success", callback))
    return "{}?{}".format(XCALLBACK_SHORTCUTS, urllib.parse.urlencode(query))


def shortcut_run_wait(name: str, input_text: str = "", *, callback: str = "pyto://") -> CapabilityResult:
    """Run a Shortcut and ask iOS to send its output back via x-callback-url."""
    started = time.monotonic()
    name = (name or "").strip()
    if not name:
        return record(
            CapabilityResult(
                action="shortcut_run_wait",
                ok=False,
                supported=False,
                method="none",
                detail="a Shortcut name is required",
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        )
    url = shortcut_wait_url(name, input_text, callback=callback)
    opener = _optional_attr("xcallback", "open_url")
    method = "xcallback.open_url"
    if callable(opener):
        try:
            opener(url)
            opened = CapabilityResult(action="open_url", ok=True, supported=True, method=method, detail="opened")
        except Exception as exc:  # noqa: BLE001 - fall back to the plain scheme
            opened = open_url(url)
            method = "webbrowser"
            if not opened.ok:
                opened = CapabilityResult(
                    action="open_url",
                    ok=False,
                    supported=False,
                    method=method,
                    detail="{}: {}".format(type(exc).__name__, exc),
                )
    else:
        opened = open_url(url)
        method = "webbrowser"
    return record(
        CapabilityResult(
            action="shortcut_run_wait",
            ok=opened.ok,
            supported=opened.supported,
            method=method,
            detail=(
                "asked iOS to run {!r} and to return its output to {}".format(name, callback)
                if opened.ok
                else "could not hand the x-callback URL to iOS ({})".format(opened.detail)
            ),
            data={"name": name, "url": url, "callback": callback, "url_opened": opened.ok},
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    )
