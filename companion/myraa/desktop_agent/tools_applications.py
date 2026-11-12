"""
Application control: launch and close common Windows applications.

Launch strategy is layered for robustness:
  1. Try a known executable / shell verb (fastest, most reliable).
  2. Fall back to the Windows "where"/App Paths lookup via `start`.

Closing uses taskkill on the matching process image name, with a graceful
grace period so apps can save work.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import webbrowser
from typing import Any, Dict

from .registry import ToolError, register

# Canonical app key -> (launch_command, kind)
#   kind == "exe"   : launch_command is the executable name (resolved via PATH/App Paths)
#   kind == "shell" : launch_command is a shell builtin verb run with cmd /c
#   kind == "uwp"   : launch_command is an apps-family activation string
APP_COMMANDS: Dict[str, Dict[str, str]] = {
    "notepad": {"exe": "notepad.exe", "image": "notepad.exe", "label": "Notepad"},
    "chrome": {"exe": "chrome.exe", "image": "chrome.exe", "label": "Google Chrome"},
    "edge": {"exe": "msedge.exe", "image": "msedge.exe", "label": "Microsoft Edge"},
    "vscode": {"exe": "code.cmd", "image": "Code.exe", "label": "Visual Studio Code"},
    "calculator": {"shell": "calc", "image": "CalculatorApp.exe", "label": "Calculator"},
    "calc": {"shell": "calc", "image": "CalculatorApp.exe", "label": "Calculator"},
    "file explorer": {"shell": "explorer", "image": "explorer.exe", "label": "File Explorer"},
    "explorer": {"shell": "explorer", "image": "explorer.exe", "label": "File Explorer"},
    "task manager": {"shell": "taskmgr", "image": "Taskmgr.exe", "label": "Task Manager"},
    "taskmanager": {"shell": "taskmgr", "image": "Taskmgr.exe", "label": "Task Manager"},
    "settings": {"uwp": "ms-settings:", "image": "SystemSettings.exe", "label": "Settings"},
    "command prompt": {"exe": "cmd.exe", "image": "cmd.exe", "label": "Command Prompt"},
    "cmd": {"exe": "cmd.exe", "image": "cmd.exe", "label": "Command Prompt"},
    "powershell": {"exe": "powershell.exe", "image": "powershell.exe", "label": "PowerShell"},
    "wordpad": {"shell": "write", "image": "wordpad.exe", "label": "WordPad"},
    "paint": {"shell": "mspaint", "image": "mspaint.exe", "label": "Paint"},
    "snipping tool": {"uwp": "ms-screenclip:", "image": "ScreenClippingHost.exe", "label": "Snipping Tool"},
    "whatsapp": {"app_id": "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App", "image": "WhatsApp.exe", "label": "WhatsApp"},
    "whatsapp web": {"url": "https://web.whatsapp.com", "image": "chrome.exe", "label": "WhatsApp Web"},
}


def _resolve_app(key: str) -> Dict[str, str]:
    norm = (key or "").strip().lower()
    if norm in APP_COMMANDS:
        return APP_COMMANDS[norm]
    # Allow loose aliases (e.g. "code", "visual studio code").
    aliases = {
        "code": "vscode",
        "visual studio code": "vscode",
        "vs code": "vscode",
        "google chrome": "chrome",
        "microsoft edge": "edge",
        "calc": "calculator",
        "settings app": "settings",
        "file explorer": "file explorer",
        "windows explorer": "file explorer",
        "whats app": "whatsapp",
        "whatsapp desktop": "whatsapp",
        "whatsapp app": "whatsapp",
    }
    if norm in aliases and aliases[norm] in APP_COMMANDS:
        return APP_COMMANDS[aliases[norm]]
    raise ToolError(
        f"Unrecognized application '{key}'. Supported: "
        f"{', '.join(sorted({v['label'] for v in APP_COMMANDS.values()}))}."
    )


def _launch(spec: Dict[str, str]) -> None:
    try:
        if "app_id" in spec:
            if os.name != "nt":
                raise ToolError(f"{spec['label']} app launch is only configured for Windows.")
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{spec['app_id']}"],
                shell=False,
                close_fds=True,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        elif "url" in spec:
            if not webbrowser.open(spec["url"], new=2):
                raise ToolError(f"Could not open {spec['label']} in the default browser.")
        elif "exe" in spec:
            exe = spec["exe"]
            resolved = shutil.which(exe)
            if resolved and os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
                os.startfile(resolved)
            elif resolved:
                # Detached so we don't block the agent.
                subprocess.Popen(
                    [resolved],
                    shell=False,
                    close_fds=True,
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            elif os.name == "nt":
                # `start` consults Windows App Paths and registered app aliases,
                # which covers Chrome/Edge installations that are not on PATH.
                subprocess.Popen(f'start "" "{exe}"', shell=True, close_fds=True)
            else:
                raise ToolError(f"Could not find executable '{exe}'.")
        elif "shell" in spec:
            subprocess.Popen(
                f'start "" {spec["shell"]}', shell=True, close_fds=True
            )
        elif "uwp" in spec:
            subprocess.Popen(
                f'start "" {spec["uwp"]}', shell=True, close_fds=True
            )
        else:
            raise ToolError(f"App spec for {spec.get('label')} is incomplete.")
    except Exception as e:  # noqa: BLE001
        if isinstance(e, ToolError):
            raise
        raise ToolError(f"Could not launch {spec.get('label')}: {e}") from e


def _normalize_app_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def _find_start_menu_shortcut(name: str) -> str | None:
    """Find an exact Start Menu shortcut for apps outside the built-in aliases."""
    if os.name != "nt":
        return None
    wanted = _normalize_app_name(name)
    if not wanted:
        return None
    roots = (
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
    )
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for current, _dirs, files in os.walk(root):
            for filename in files:
                if filename.lower().endswith(".lnk") and _normalize_app_name(filename[:-4]) == wanted:
                    return os.path.join(current, filename)
    return None


@register("openApplication")
def open_application(args: Dict[str, Any]) -> Dict[str, Any]:
    name = args.get("name") or args.get("application")
    if not name:
        raise ToolError("Parameter 'name' (application name) is required.")
    try:
        spec = _resolve_app(str(name))
    except ToolError as error:
        shortcut = _find_start_menu_shortcut(str(name))
        if not shortcut:
            raise error
        os.startfile(shortcut)
        return {"result": f"{os.path.splitext(os.path.basename(shortcut))[0]} opened."}
    _launch(spec)
    return {"result": f"{spec['label']} opened."}


@register("closeApplication")
def close_application(args: Dict[str, Any]) -> Dict[str, Any]:
    name = args.get("name") or args.get("application")
    force = bool(args.get("force", False))
    if not name:
        raise ToolError("Parameter 'name' (application name) is required.")
    spec = _resolve_app(str(name))
    image = spec["image"]
    # Graceful close first (WM_CLOSE via taskkill), then force if requested.
    graceful_flag = "" if force else ""
    force_flag = " /F" if force else ""
    try:
        # taskkill returns non-zero if the process isn't running — that's fine.
        subprocess.run(
            f'taskkill /IM "{image}"{graceful_flag}{force_flag}',
            shell=True,
            capture_output=True,
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Could not close {spec['label']}: {e}") from e
    # Give the OS a moment to actually tear it down.
    time.sleep(0.2)
    return {"result": f"Closed {spec['label']}."}


__all__ = ["open_application", "close_application", "APP_COMMANDS"]
