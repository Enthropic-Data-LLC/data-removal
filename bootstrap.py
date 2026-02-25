#!/usr/bin/env python3
"""
Cross-platform setup for the Data Removal CLI.

Works on: Ubuntu/Debian, Fedora/RHEL, Arch, macOS, Windows
Requires: Python 3.11+

Usage:
    python bootstrap.py             # Full setup
    python bootstrap.py --check     # Check prerequisites only
    python bootstrap.py --browser   # Also install Playwright browsers
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_PYTHON = (3, 11)
VENV_DIR = ".venv"

# Colors (disabled on Windows unless in modern terminal)
if sys.platform == "win32" and not os.environ.get("WT_SESSION"):
    GREEN = YELLOW = RED = NC = ""
else:
    GREEN, YELLOW, RED, NC = "\033[0;32m", "\033[1;33m", "\033[0;31m", "\033[0m"


def info(msg: str) -> None:
    print(f"{GREEN}[✓]{NC} {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}[!]{NC} {msg}")


def fail(msg: str) -> None:
    print(f"{RED}[✗]{NC} {msg}")
    sys.exit(1)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, printing it on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        warn(f"Command failed: {' '.join(cmd)}")
        if result.stderr:
            print(f"    {result.stderr.strip()}")
    return result


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def get_platform() -> str:
    s = sys.platform
    if s.startswith("linux"):
        return "linux"
    elif s == "darwin":
        return "macos"
    elif s == "win32":
        return "windows"
    return s


def get_linux_distro() -> str:
    """Detect Linux distro family."""
    try:
        with open("/etc/os-release") as f:
            content = f.read().lower()
        if any(d in content for d in ("ubuntu", "debian", "pop", "mint", "elementary")):
            return "debian"
        elif any(d in content for d in ("fedora", "rhel", "centos", "rocky", "alma")):
            return "fedora"
        elif any(d in content for d in ("arch", "manjaro", "endeavour")):
            return "arch"
        elif "suse" in content:
            return "suse"
    except FileNotFoundError:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# System dependency checks
# ---------------------------------------------------------------------------

def check_python() -> bool:
    ver = sys.version_info[:2]
    if ver >= MIN_PYTHON:
        info(f"Python {ver[0]}.{ver[1]} — OK")
        return True
    else:
        fail(
            f"Python {ver[0]}.{ver[1]} found, but {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required.\n"
            f"    Install a newer Python and re-run this script."
        )
        return False


def check_venv_module() -> bool:
    """Check if venv module is available (not always on Linux)."""
    try:
        import ensurepip  # noqa: F401
        return True
    except ImportError:
        return False


def install_system_deps(plat: str, install_browser: bool) -> None:
    """Install system packages if needed."""

    if plat == "linux":
        distro = get_linux_distro()
        info(f"Detected Linux distro family: {distro}")

        if not check_venv_module():
            warn("Python venv module missing — installing...")
            ver = f"{sys.version_info.major}.{sys.version_info.minor}"
            if distro == "debian":
                run(["sudo", "apt-get", "update", "-qq"])
                run(["sudo", "apt-get", "install", "-y", "-qq",
                     f"python{ver}-venv", f"python{ver}-dev", "build-essential"])
            elif distro == "fedora":
                run(["sudo", "dnf", "install", "-y", "-q",
                     f"python{ver}-devel", "gcc"])
            elif distro == "arch":
                # Arch includes venv with python
                run(["sudo", "pacman", "-S", "--noconfirm", "--needed", "python", "base-devel"])
            else:
                warn(f"Unknown distro — install python3-venv manually")

        if install_browser:
            info("Installing Playwright system dependencies...")
            if distro == "debian":
                run(["sudo", "apt-get", "install", "-y", "-qq",
                     "libnss3", "libatk1.0-0", "libatk-bridge2.0-0",
                     "libcups2", "libdrm2", "libxkbcommon0",
                     "libxcomposite1", "libxdamage1", "libxrandr2",
                     "libgbm1", "libpango-1.0-0", "libcairo2", "libasound2t64"])
            # Playwright's own installer handles most deps on other distros

    elif plat == "macos":
        # Check for Xcode command line tools (needed for compilation)
        result = run(["xcode-select", "-p"])
        if result.returncode != 0:
            warn("Xcode command line tools not found. Installing...")
            run(["xcode-select", "--install"])
            info("Follow the dialog to install, then re-run this script.")
            sys.exit(0)
        else:
            info("Xcode CLI tools — OK")

    elif plat == "windows":
        # Python on Windows typically comes with everything needed
        info("Windows detected — no system packages needed")
        # Check for Visual C++ build tools if we need compiled deps
        if not shutil.which("cl"):
            warn(
                "Visual C++ build tools not found. If pip install fails,\n"
                "    install from: https://visualstudio.microsoft.com/visual-cpp-build-tools/"
            )


# ---------------------------------------------------------------------------
# Venv + project install
# ---------------------------------------------------------------------------

def venv_python() -> str:
    """Path to the venv Python executable."""
    if sys.platform == "win32":
        return str(Path(VENV_DIR) / "Scripts" / "python.exe")
    return str(Path(VENV_DIR) / "bin" / "python")


def venv_pip() -> str:
    if sys.platform == "win32":
        return str(Path(VENV_DIR) / "Scripts" / "pip.exe")
    return str(Path(VENV_DIR) / "bin" / "pip")


def venv_bin(name: str) -> str:
    if sys.platform == "win32":
        return str(Path(VENV_DIR) / "Scripts" / f"{name}.exe")
    return str(Path(VENV_DIR) / "bin" / name)


def create_venv() -> None:
    if Path(VENV_DIR).exists():
        info(f"Virtual environment exists ({VENV_DIR})")
        return

    info("Creating virtual environment...")
    venv.create(VENV_DIR, with_pip=True)
    info(f"Virtual environment created ({VENV_DIR})")


def install_project(install_browser: bool) -> None:
    info("Upgrading pip...")
    run([venv_python(), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel", "-q"])

    info("Installing data-removal CLI...")
    result = run([venv_pip(), "install", "-e", ".[dev]", "-q"])
    if result.returncode != 0:
        fail("pip install failed. See errors above.")
    info("Package installed")

    if install_browser:
        info("Installing Playwright + Chromium...")
        run([venv_pip(), "install", "playwright", "-q"])
        run([venv_python(), "-m", "playwright", "install", "chromium", "--with-deps"])
        info("Playwright ready")


def run_tests() -> bool:
    info("Running tests...")
    result = subprocess.run(
        [venv_python(), "-m", "pytest", "tests/", "-v", "--tb=short"],
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_success(plat: str) -> None:
    print()
    print("═" * 52)
    info("Setup complete!")
    print("═" * 52)
    print()

    if plat == "windows":
        activate = f"  Activate venv:    {VENV_DIR}\\Scripts\\activate"
    else:
        activate = f"  Activate venv:    source {VENV_DIR}/bin/activate"

    print(activate)
    print("  CLI help:         dr --help")
    print("  Add a profile:    dr profile add")
    print("  List brokers:     dr brokers list")
    print("  Run tests:        pytest tests/ -v")
    print()

    # Show data location
    try:
        # Import from the installed package to show actual path
        result = run([
            venv_python(), "-c",
            "from dataremoval.core.database import default_db_path; print(default_db_path())"
        ])
        if result.returncode == 0 and result.stdout.strip():
            print(f"  Data stored at:   {result.stdout.strip()}")
            print()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Set up the Data Removal CLI")
    parser.add_argument("--check", action="store_true", help="Check prerequisites only")
    parser.add_argument("--browser", action="store_true", help="Install Playwright browsers")
    args = parser.parse_args()

    plat = get_platform()

    print()
    print("═" * 52)
    print("  Data Removal CLI — Setup")
    print(f"  Platform: {platform.system()} {platform.release()}")
    print(f"  Python:   {platform.python_version()}")
    print("═" * 52)
    print()

    # Pre-checks
    check_python()

    if args.check:
        install_system_deps(plat, install_browser=False)
        info("Prerequisites look good!")
        return

    # Full setup
    install_system_deps(plat, install_browser=args.browser)
    create_venv()
    install_project(install_browser=args.browser)

    if run_tests():
        print_success(plat)
    else:
        warn("Some tests failed — setup may still be usable")
        print_success(plat)


if __name__ == "__main__":
    main()
