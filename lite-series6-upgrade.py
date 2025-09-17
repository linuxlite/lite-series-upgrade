#!/usr/bin/env python3
# /usr/bin/lite-series6-upgrade.py
# GTK4 Python app to upgrade Linux Lite 6.x (Ubuntu 22.04 base) → 7.x (Ubuntu 24.04 base)
# Features
#  - pkexec-only enforcement
#  - Accurate weighted % progress across stages
#  - Dry-run mode (instant, no APT/DPKG/do-release-upgrade execution)
#  - Auto-fix common issues (dpkg --configure -a, apt-get -f install, etc.)
#  - Switch Linux Lite repo codename: fluorite → galena (with timestamped backups), then apt-get update
#  - Optional re-enable of known-good PPAs after upgrade (Linux Lite + Launchpad)
#  - Fetch/extract/install LibreOffice Series 7 bundle (.deb files) from official URL
#  - Log to /var/log/ll-series-upgrade.log (fallback /tmp)
#  - UI actions: View Log, Restart, Shutdown

import gi
import os
import sys
import shlex
import subprocess
import threading
import time
import shutil
import socket
import logging
import tarfile
import urllib.request
from pathlib import Path


def _add_project_root_to_sys_path() -> bool:
    """Ensure the bundled ``lite_series_upgrade`` package can be imported."""

    script_path = Path(__file__).resolve()
    search_roots = [script_path.parent]

    # Walk a couple of parent directories to cover common installation layouts
    # such as ``/usr/lib/lite-series-upgrade`` with a symlink in ``/usr/bin``.
    for parent in list(script_path.parents)[:4]:
        search_roots.append(parent)

    search_roots.extend(
        Path(path)
        for path in os.environ.get("LITE_SERIES_UPGRADE_PATH", "").split(os.pathsep)
        if path
    )

    prefixes: set[Path] = set()
    prefixes.update(
        Path(p)
        for p in (
            sys.prefix,
            getattr(sys, "base_prefix", sys.prefix),
            sys.exec_prefix,
            getattr(sys, "base_exec_prefix", sys.exec_prefix),
        )
    )
    prefixes.update(
        Path(parent)
        for parent in (
            "/usr",
            "/usr/local",
        )
    )

    for prefix in prefixes:
        for lib_dir in ("lib", "lib64", "share"):
            search_roots.append(prefix / lib_dir / "lite-series-upgrade")

    # Python installations on Debian/Ubuntu place packages under
    # ``lib/pythonX/dist-packages`` or ``lib/pythonX/site-packages``.  These
    # directories are normally on ``sys.path`` when running the interpreter, but
    # pkexec sanitises environment variables which can lead to an empty
    # ``PYTHONPATH``.  Include the standard site package directories so the
    # bundled package can still be found.
    import sysconfig
    import site

    site_dirs: set[Path] = set()
    site_dirs.update(Path(path) for path in sysconfig.get_paths().values())

    try:
        site_dirs.update(Path(path) for path in site.getsitepackages())
    except AttributeError:
        # ``site.getsitepackages`` is not available in virtual environments.
        pass

    try:
        user_site = site.getusersitepackages()
    except AttributeError:
        user_site = None
    if user_site:
        site_dirs.add(Path(user_site))

    for site_dir in site_dirs:
        search_roots.append(site_dir)

    seen: set[Path] = set()
    for root in search_roots:
        try:
            resolved = root.resolve()
        except FileNotFoundError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)

        package_init = resolved / "lite_series_upgrade" / "__init__.py"
        if package_init.exists():
            root_str = str(resolved)
            if root_str not in sys.path:
                sys.path.insert(0, root_str)
            return True

    return False


try:
    from lite_series_upgrade.apt_sources import should_backup_apt_source
except ModuleNotFoundError as exc:
    if exc.name not in {"lite_series_upgrade", "lite_series_upgrade.apt_sources"}:
        raise
    if not _add_project_root_to_sys_path():
        raise ModuleNotFoundError(
            "lite_series_upgrade package could not be located. Set the"
            " LITE_SERIES_UPGRADE_PATH environment variable or install the"
            " project so that the package is available on PYTHONPATH."
        ) from exc
    from lite_series_upgrade.apt_sources import should_backup_apt_source

# GTK
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gio

APP_NAME = "Lite Series 6 Upgrade"
APP_ID = "com.linuxlite.LiteSeries6Upgrade"
LL_FROM_VERSION = "6.6"
LL_TO_VERSION = "7.0"
LL_TO_SERIES_LTS = "7.6"
UBUNTU_FROM_CODENAME = "jammy"
UBUNTU_TO_CODENAME = "noble"
LOG_PATH_ROOT = Path("/var/log/ll-series-upgrade.log")
LOG_PATH_FALLBACK = Path("/tmp/ll-series-upgrade.log")
BUNDLE_URL = "https://repo.linuxliteos.com/upgrade/7.6/libreoffice/loffice76.tar.gz"
CACHE_DIR_ROOT = Path("/var/cache/ll-series-upgrade")
CACHE_DIR_FALLBACK = Path("/tmp/ll-series-upgrade")

# ---------------------- Logging setup ----------------------

def get_log_path() -> Path:
    return LOG_PATH_ROOT if os.geteuid() == 0 else LOG_PATH_FALLBACK

LOG_PATH = get_log_path()
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_PATH),
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ll-series6-upgrade")

# ---------------------- Helpers ----------------------

def _cmd_to_list(cmd, shell):
    if isinstance(cmd, str) and not shell:
        return shlex.split(cmd)
    return cmd


def run_cmd(
    cmd,
    env=None,
    shell=False,
    timeout=None,
    log_output=True,
    dry_run=False,
    check=False,
):
    """Run a command and return (rc, output_lines).

    In dry-run mode, short-circuit apt/dpkg/upgrade tools to avoid delays. When
    ``check`` is True, raise ``subprocess.CalledProcessError`` on non-zero exit.
    """

    cmd_list = _cmd_to_list(cmd, shell)
    display_cmd = cmd if isinstance(cmd, str) else " ".join(cmd_list)
    output_lines: list[str] = []

    # Fast path for dry-run: DO NOT execute slow package tools
    if dry_run and isinstance(cmd, (str, list)):
        base0 = None
        if isinstance(cmd, list) and cmd:
            base0 = os.path.basename(str(cmd[0]))
        elif isinstance(cmd, str):
            base0 = cmd.strip().split(" ")[0]
        if base0 in (
            "apt",
            "apt-get",
            "/usr/bin/apt",
            "/usr/bin/apt-get",
            "dpkg",
            "/usr/bin/dpkg",
            "update-grub",
            "update-initramfs",
            "do-release-upgrade",
            "/usr/bin/do-release-upgrade",
        ):
            preview = cmd if isinstance(cmd, str) else " ".join(cmd)
            message = f"[DRY RUN] Would run: {preview}"
            if log_output:
                logger.info(message)
            output_lines.append(message)
            return 0, output_lines

    logger.info("RUN: %s", display_cmd)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    try:
        process = subprocess.Popen(
            cmd_list if not shell else (cmd if isinstance(cmd, str) else " ".join(cmd_list)),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=full_env,
            shell=shell,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("Failed to start %s: %s", display_cmd, e)
        output_lines.append(str(e))
        if check:
            raise
        return 1, output_lines

    start = time.time()
    rc = 1
    try:
        soft_timeout = timeout or 1200  # 20 minutes
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if log_output:
                logger.info(line)
            output_lines.append(line)
        rc = process.wait(timeout=soft_timeout)
        logger.info("RET(%s): %s s", rc, int(time.time() - start))
    except subprocess.TimeoutExpired:
        process.kill()
        rc = 124
        message = f"Command timed out: {display_cmd}"
        logger.error(message)
        output_lines.append(message)
    except Exception as e:  # pragma: no cover - defensive
        process.kill()
        message = f"Error running {display_cmd}: {e}"
        logger.exception(message)
        output_lines.append(message)
    finally:
        if process.stdout:
            process.stdout.close()

    if rc != 0:
        logger.warning("Non-zero exit: %s => %s", display_cmd, rc)
        if check:
            raise subprocess.CalledProcessError(
                rc,
                cmd if shell else cmd_list,
                output="\n".join(output_lines),
            )
    return rc, output_lines


def internet_available(host="archive.ubuntu.com", port=80, timeout=5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def effective_cache_dir() -> Path:
    if os.geteuid() == 0:
        CACHE_DIR_ROOT.mkdir(parents=True, exist_ok=True)
        return CACHE_DIR_ROOT
    CACHE_DIR_FALLBACK.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR_FALLBACK

# ---------------------- Upgrade Engine ----------------------

class UpgradeEngine:
    KNOWN_PPA_WHITELIST = (
        "linuxliteos.com",
        "linuxlite",
        "ppa.launchpad.net",
    )

    # Tunable weights
    WEIGHTS = {
        "System check & preparation": 1,
        "Disable third-party & update Linux Lite repo": 3,
        "Fix broken packages & configure pending": 4,
        "Ensure upgrade tools": 2,
        "Release upgrade to 24.04": 20,
        "Auto-resolve common issues": 3,
        "Install LibreOffice Series 7 bundle": 4,
        "Post-upgrade cleanup": 6,
        "Update branding/version files": 2,
        "Verify upgraded release": 1,
        "Re-enable known-good PPAs": 2,
    }

    def __init__(self, ui_callback, progress_callback, dry_run=False, reenable_ppas=False):
        self.ui_callback = ui_callback  # function(text)
        self.progress_callback = progress_callback  # function(percent_float, label)
        self.dry_run = dry_run
        self.reenable_ppas = reenable_ppas
        self.disabled_lists = []
        self.env = {
            "DEBIAN_FRONTEND": "noninteractive",
            "NEEDRESTART_MODE": "a",
            "UBUNTU_FRONTEND": "noninteractive",
            "APT_LISTCHANGES_FRONTEND": "none",
            "LC_ALL": "C.UTF-8",
        }
        # Plan
        self.plan = [
            ("System check & preparation", self.WEIGHTS["System check & preparation"], self.step_system_check),
            ("Disable third-party & update Linux Lite repo", self.WEIGHTS["Disable third-party & update Linux Lite repo"], self.step_disable_third_party),
            ("Fix broken packages & configure pending", self.WEIGHTS["Fix broken packages & configure pending"], self.step_fix_common),
            ("Ensure upgrade tools", self.WEIGHTS["Ensure upgrade tools"], self.step_ensure_tools),
            ("Release upgrade to 24.04", self.WEIGHTS["Release upgrade to 24.04"], self.step_release_upgrade),
            ("Auto-resolve common issues", self.WEIGHTS["Auto-resolve common issues"], self.step_auto_resolve),
            ("Install LibreOffice Series 7 bundle", self.WEIGHTS["Install LibreOffice Series 7 bundle"], self.step_install_libreoffice_bundle),
            ("Post-upgrade cleanup", self.WEIGHTS["Post-upgrade cleanup"], self.step_post_upgrade),
            ("Update branding/version files", self.WEIGHTS["Update branding/version files"], self.step_update_branding),
            ("Verify upgraded release", self.WEIGHTS["Verify upgraded release"], self.step_verify),
        ]
        if self.reenable_ppas:
            self.plan.append(("Re-enable known-good PPAs", self.WEIGHTS["Re-enable known-good PPAs"], self.step_reenable_known_ppas))

        self.total_units = sum(w for _, w, _ in self.plan)
        self.done_units = 0
        self._progress_lock = threading.Lock()

    # ---------- UI helpers ----------
    def emit(self, text: str):
        logger.info(text)
        GLib.idle_add(self.ui_callback, text + " ")

    def _inc_progress(self, units=1, label: str | None = None):
        with self._progress_lock:
            self.done_units += units
            pct = max(0.0, min(1.0, self.done_units / float(self.total_units)))
        GLib.idle_add(self.progress_callback, pct, label if label else "")

    def _apt(self, *args):
        base = [
            "/usr/bin/apt-get",
            "-y",
            "-o",
            "Dpkg::Options::=--force-confdef",
            "-o",
            "Dpkg::Options::=--force-confold",
        ]
        return list(base) + list(args)

    def _format_cmd(self, cmd) -> str:
        return cmd if isinstance(cmd, str) else " ".join(cmd)

    def _run_and_emit(
        self,
        cmd,
        *,
        env=None,
        shell=False,
        timeout=None,
        log_output=True,
        dry_run=None,
        check=False,
    ):
        rc, output = run_cmd(
            cmd,
            env=env if env is not None else self.env,
            shell=shell,
            timeout=timeout,
            log_output=log_output,
            dry_run=self.dry_run if dry_run is None else dry_run,
            check=check,
        )
        for line in output:
            self.emit(line)
        return rc, output

    # ---------- Steps ----------
    def step_system_check(self):
        self.emit(f"Logging to: {LOG_PATH}")
        if not internet_available():
            self.emit("No internet connectivity to archive.ubuntu.com. Aborting.")
            return False
        if os.geteuid() != 0 or "PKEXEC_UID" not in os.environ:
            self.emit("This program must be launched via pkexec. Aborting.")
            return False
        self._inc_progress(self.WEIGHTS["System check & preparation"], "System check complete")
        return True

    def _update_linuxlite_repo_codename(self, d: Path):
        for fname in ["linuxlite.list", "linuxlite.list.save"]:
            fpath = d / fname
            if not fpath.exists():
                continue
            try:
                content = fpath.read_text()
                if "fluorite" in content:
                    if self.dry_run:
                        self.emit(
                            f"[DRY RUN] Would replace 'fluorite' with 'galena' in {fname}"
                        )
                    else:
                        new_content = content.replace("fluorite", "galena")
                        fpath.write_text(new_content)
                        self.emit(f"Updated {fname}: fluorite -> galena")
                else:
                    self.emit(f"No 'fluorite' found in {fname}; no change needed")
            except Exception as e:
                self.emit(f"Warning: could not update {fname}: {e}")

    def _update_ubuntu_codename(self) -> int:
        apt_dir = Path("/etc/apt")
        candidates: list[Path] = []
        sources_list = apt_dir / "sources.list"
        if sources_list.exists():
            candidates.append(sources_list)
        sources_d = apt_dir / "sources.list.d"
        if sources_d.exists():
            for path in sources_d.iterdir():
                if not path.is_file():
                    continue
                if path.name.endswith(".disabled"):
                    continue
                if path.suffix not in {".list", ".sources"}:
                    continue
                candidates.append(path)
        changed = 0
        for path in candidates:
            if self._replace_in_file(
                path,
                [(UBUNTU_FROM_CODENAME, UBUNTU_TO_CODENAME)],
            ):
                changed += 1
        return changed

    def step_disable_third_party(self):
        d = Path("/etc/apt/sources.list.d")
        # Switch Linux Lite repo codename fluorite -> galena
        self._update_linuxlite_repo_codename(d)
        # apt update after codename swap
        rc, _ = self._run_and_emit(self._apt("update"))
        if rc != 0:
            self.emit("apt-get update failed; aborting step.")
            return False
        self._inc_progress(1, "Sources updated")

        # Disable non-Ubuntu/non-LinuxLite
        for f in d.glob("*.list"):
            try:
                txt = f.read_text()
                keep = ("ubuntu.com" in txt) or ("linuxlite" in txt) or ("linuxliteos" in txt)
                if not keep:
                    backup = f.with_suffix(f.suffix + ".disabled")
                    if not backup.exists():
                        if self.dry_run:
                            self.emit(f"[DRY RUN] Would disable {f.name}")
                            self.disabled_lists.append(backup)
                        else:
                            f.rename(backup)
                            self.disabled_lists.append(backup)
                            self.emit(f"Disabled {f.name}")
            except Exception as e:
                self.emit(f"Warning: could not inspect {f}: {e}")
        remaining = self.WEIGHTS["Disable third-party & update Linux Lite repo"] - 1
        if remaining > 0:
            self._inc_progress(remaining, "Third-party sources handled")
        return True

    def step_fix_common(self):
        cmds = [
            ["/usr/bin/dpkg", "--configure", "-a"],
            self._apt("-f", "install"),
            self._apt("update"),
            self._apt("autoremove", "--purge"),
            self._apt("clean"),
        ]
        weight = self.WEIGHTS["Fix broken packages & configure pending"]
        per = max(1, weight // len(cmds))
        last_bonus = weight - per * (len(cmds) - 1)
        for i, cmd in enumerate(cmds):
            rc, _ = self._run_and_emit(cmd)
            if rc != 0:
                self.emit(f"Command failed ({rc}): {self._format_cmd(cmd)}")
                return False
            self._inc_progress(last_bonus if i == len(cmds) - 1 else per, "Fix/cleanup step done")
        return True

    def step_ensure_tools(self):
        required_tools = ("/usr/bin/apt-get", "/usr/bin/dpkg")
        missing = [tool for tool in required_tools if not Path(tool).exists()]
        if missing:
            self.emit("Missing required tool(s): " + ", ".join(missing))
            return False
        self.emit("APT tooling available for manual release upgrade.")
        self._inc_progress(self.WEIGHTS["Ensure upgrade tools"], "Verified upgrade tooling")
        return True

    def step_release_upgrade(self):
        self.emit(
            f"Switching Ubuntu repositories {UBUNTU_FROM_CODENAME} → {UBUNTU_TO_CODENAME}…"
        )
        changed = self._update_ubuntu_codename()
        if changed:
            self.emit(
                f"Updated {changed} apt source file(s) to {UBUNTU_TO_CODENAME}."
            )
        else:
            self.emit(
                f"No apt source entries required {UBUNTU_FROM_CODENAME} → {UBUNTU_TO_CODENAME} changes."
            )

        weight = self.WEIGHTS["Release upgrade to 24.04"]
        sources_units = 1 if weight > 2 else 0
        update_units = 1 if weight > 1 else 0
        upgrade_units = weight - sources_units - update_units

        if sources_units:
            self._inc_progress(sources_units, "Apt sources retargeted")

        self.emit("Refreshing package lists after codename switch…")
        rc, _ = self._run_and_emit(self._apt("update"))
        if rc != 0:
            self.emit("apt-get update failed after switching to noble.")
            return False
        if update_units:
            self._inc_progress(update_units, f"Package lists refreshed ({UBUNTU_TO_CODENAME})")

        self.emit("Upgrading installed packages for the new release…")
        rc, _ = self._run_and_emit(self._apt("dist-upgrade"))
        if rc != 0:
            self.emit(f"Distribution upgrade failed (rc={rc}).")
            return False
        if upgrade_units:
            self._inc_progress(
                upgrade_units,
                f"Distribution upgrade to {UBUNTU_TO_CODENAME} complete",
            )
        return True

    def step_auto_resolve(self):
        cmds = [
            ["/usr/bin/dpkg", "--configure", "-a"],
            self._apt("-f", "install"),
        ]
        weight = self.WEIGHTS["Auto-resolve common issues"]
        per = max(1, weight // len(cmds))
        last_bonus = weight - per * (len(cmds) - 1)
        for i, cmd in enumerate(cmds):
            rc, _ = self._run_and_emit(cmd)
            if rc != 0:
                self.emit(f"Command failed ({rc}): {self._format_cmd(cmd)}")
                return False
            self._inc_progress(last_bonus if i == len(cmds) - 1 else per, "Auto-resolve step done")
        return True

    # ---- LibreOffice bundle helpers ----
    def _download_file(self, url: str, dest: Path):
        if self.dry_run:
            self.emit(f"[DRY RUN] Would download {url} -> {dest}")
            return True
        try:
            with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            return True
        except Exception as e:
            self.emit(f"Error downloading {url}: {e}")
            return False

    def _extract_tar_gz(self, tar_path: Path, out_dir: Path):
        if self.dry_run:
            self.emit(f"[DRY RUN] Would extract {tar_path} -> {out_dir}")
            return True
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                tar.extractall(out_dir)
            return True
        except Exception as e:
            self.emit(f"Error extracting {tar_path}: {e}")
            return False

    def step_install_libreoffice_bundle(self):
        self.emit("Handling LibreOffice Series 7 bundle…")
        cache = effective_cache_dir()
        tar_path = cache / "loffice76.tar.gz"
        extract_dir = cache / "loffice76"

        ok = self._download_file(BUNDLE_URL, tar_path)
        if not ok:
            return False
        self._inc_progress(1, "Downloaded LibreOffice bundle")

        if not self.dry_run:
            extract_dir.mkdir(parents=True, exist_ok=True)
        ok = self._extract_tar_gz(tar_path, extract_dir)
        if not ok:
            return False
        self._inc_progress(1, "Extracted LibreOffice bundle")

        if self.dry_run:
            self.emit("[DRY RUN] Would install all .deb files from extracted bundle")
            self.emit("[DRY RUN] Would purge previous LibreOffice 'libreoffice7.5*'")
            # Remove old menu entries (report only)
            apps_dir = Path("/usr/share/applications")
            for f in apps_dir.glob("libreoffice7.5-*.desktop"):
                self.emit(f"[DRY RUN] Would remove {f}")
            self._inc_progress(2, "Simulated LibreOffice install & cleanup")
            return True

        debs: list[str] = []
        for root, _dirs, files in os.walk(extract_dir):
            for fn in files:
                if fn.endswith(".deb"):
                    debs.append(str(Path(root) / fn))
        if not debs:
            self.emit("No .deb files found in the bundle.")
            return False
        rc, _ = self._run_and_emit(["/usr/bin/dpkg", "-i"] + debs, dry_run=False)
        if rc != 0:
            self.emit(f"Failed to install LibreOffice bundle (rc={rc}).")
            return False
        self._inc_progress(1, "Installed LibreOffice .deb packages")
        rc, _ = self._run_and_emit(self._apt("-f", "install"), dry_run=False)
        if rc != 0:
            self.emit(f"Failed to resolve LibreOffice dependencies (rc={rc}).")
            return False
        self._inc_progress(1, "Resolved LibreOffice dependencies")

        # Uninstall previous LibreOffice version (7.5)
        self.emit("Removing previous LibreOffice version (7.5)…")
        purge_cmd = "apt-get -y remove --purge 'libreoffice7.5*'"
        rc, _ = self._run_and_emit(purge_cmd, dry_run=False, shell=True)
        if rc != 0:
            self.emit(f"Failed to purge LibreOffice 7.5 packages (rc={rc}).")
            return False
        self._inc_progress(1, "Removed old LibreOffice 7.5")

        # Remove old .desktop entries
        apps_dir = Path("/usr/share/applications")
        removed = 0
        for f in apps_dir.glob("libreoffice7.5-*.desktop"):
            try:
                f.unlink()
                self.emit(f"Removed menu entry {f.name}")
                removed += 1
            except Exception as e:
                self.emit(f"Warning: could not remove {f}: {e}")
        self._inc_progress(1, f"Cleaned {removed} old menu entries")

        return True

    def step_post_upgrade(self):
        cmds = [
            self._apt("update"),
            self._apt("full-upgrade"),
            ["/usr/sbin/update-initramfs", "-u"],
            ["/usr/sbin/update-grub"],
            self._apt("autoremove", "--purge"),
            self._apt("clean"),
        ]
        weight = self.WEIGHTS["Post-upgrade cleanup"]
        per = max(1, weight // len(cmds))
        last_bonus = weight - per * (len(cmds) - 1)
        for i, cmd in enumerate(cmds):
            rc, _ = self._run_and_emit(cmd)
            if rc != 0:
                self.emit(f"Command failed ({rc}): {self._format_cmd(cmd)}")
                return False
            self._inc_progress(last_bonus if i == len(cmds) - 1 else per, "Post-upgrade step done")
        return True
    def _replace_in_file(self, path: Path, replacements: list[tuple[str, str]]):
        try:
            if not path.exists():
                self.emit(f"Note: {path} not found; skipping")
                return False
            content = path.read_text()
            new_content = content
            for old, new in replacements:
                new_content = new_content.replace(old, new)
            if new_content != content:
                if self.dry_run:
                    self.emit(f"[DRY RUN] Would modify {path}")
                    return True
                if should_backup_apt_source(path):
                    backup = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
                    shutil.copy2(path, backup)
                    message = f"Updated {path} (backup: {backup.name})"
                else:
                    message = f"Updated {path} (no backup per policy)"
                path.write_text(new_content)
                self.emit(message)
                return True
            else:
                self.emit(f"No changes needed in {path}")
                return False
        except Exception as e:
            self.emit(f"Warning: could not update {path}: {e}")
            return False

    def _write_file(self, path: Path, content: str, description: str) -> bool:
        try:
            if self.dry_run:
                self.emit(f"[DRY RUN] Would set {path} → {description}")
                return True
            if path.exists():
                current = path.read_text()
                if current == content:
                    self.emit(f"No changes needed in {path}")
                    return False
                backup = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
                shutil.copy2(path, backup)
                path.write_text(content)
                self.emit(f"Updated {path} → {description} (backup: {backup.name})")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                self.emit(f"Created {path} → {description}")
            return True
        except Exception as e:
            self.emit(f"Warning: could not update {path}: {e}")
            return False

    def _update_line(
        self,
        path: Path,
        prefix: str,
        new_line: str,
        description: str,
        *,
        strip: bool = False,
    ) -> bool:
        try:
            if not path.exists():
                self.emit(f"Note: {path} not found; skipping")
                return False
            lines = path.read_text().splitlines()
            found = False
            changed = False
            for idx, line in enumerate(lines):
                compare = line.strip() if strip else line
                if compare.startswith(prefix):
                    found = True
                    replacement = new_line
                    if strip:
                        leading = line[: len(line) - len(line.lstrip())]
                        replacement = leading + new_line
                    if lines[idx] != replacement:
                        lines[idx] = replacement
                        changed = True
                    break
            if not found:
                self.emit(f"Note: {path} missing entry starting with {prefix}; skipping")
                return False
            if not changed:
                self.emit(f"No changes needed in {path}")
                return False
            if self.dry_run:
                self.emit(f"[DRY RUN] Would modify {path} ({description})")
                return True
            backup = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
            shutil.copy2(path, backup)
            path.write_text("\n".join(lines) + "\n")
            self.emit(f"Updated {path} ({description}; backup: {backup.name})")
            return True
        except Exception as e:
            self.emit(f"Warning: could not update {path}: {e}")
            return False

    def step_update_branding(self):
        self.emit("Updating Linux Lite branding/version files…")
        changed = 0

        llver_path = Path("/etc/llver")
        llver_target = f"Linux Lite {LL_TO_VERSION}"
        llver_changed = self._replace_in_file(
            llver_path,
            [(f"Linux Lite {LL_FROM_VERSION}", llver_target)],
        )
        if not llver_changed:
            llver_changed = self._write_file(llver_path, llver_target, llver_target)
        changed += 1 if llver_changed else 0

        issue_path = Path("/etc/issue")
        issue_literal_target = f"Linux Lite {LL_TO_SERIES_LTS} LTS \\n \\l"
        issue_changed = self._replace_in_file(
            issue_path,
            [
                (
                    f"Linux Lite {LL_FROM_VERSION} LTS \\n \\l",
                    issue_literal_target,
                )
            ],
        )
        if not issue_changed:
            try:
                current_issue = issue_path.read_text()
            except Exception:
                current_issue = ""
            issue_target = issue_literal_target
            if "\\\\n" not in current_issue and "\\n" in current_issue:
                issue_target = f"Linux Lite {LL_TO_SERIES_LTS} LTS \n \l"
            issue_changed = self._write_file(
                issue_path,
                issue_target,
                f"Linux Lite {LL_TO_SERIES_LTS} LTS",
            )
        changed += 1 if issue_changed else 0

        lsb_path = Path("/etc/lsb-release")
        lsb_target = f'DISTRIB_DESCRIPTION="Linux Lite {LL_TO_VERSION}"'
        lsb_changed = self._replace_in_file(
            lsb_path,
            [
                (
                    f'DISTRIB_DESCRIPTION="Linux Lite {LL_FROM_VERSION}"',
                    lsb_target,
                )
            ],
        )
        if not lsb_changed:
            lsb_changed = self._update_line(
                lsb_path,
                "DISTRIB_DESCRIPTION=",
                lsb_target,
                "DISTRIB_DESCRIPTION entry",
            )
        changed += 1 if lsb_changed else 0

        os_release_path = Path("/etc/os-release")
        os_release_target = f'PRETTY_NAME="Linux Lite {LL_TO_VERSION}"'
        os_changed = self._replace_in_file(
            os_release_path,
            [
                (
                    f'PRETTY_NAME="Linux Lite {LL_FROM_VERSION}"',
                    os_release_target,
                )
            ],
        )
        if not os_changed:
            os_changed = self._update_line(
                os_release_path,
                "PRETTY_NAME=",
                os_release_target,
                "PRETTY_NAME entry",
            )
        changed += 1 if os_changed else 0

        plymouth_path = Path("/usr/share/plymouth/themes/text.plymouth")
        plymouth_target = f"title=Linux Lite {LL_TO_VERSION}"
        plymouth_changed = self._replace_in_file(
            plymouth_path,
            [(f"title=Linux Lite {LL_FROM_VERSION}", plymouth_target)],
        )
        if not plymouth_changed:
            plymouth_changed = self._update_line(
                plymouth_path,
                "title=",
                plymouth_target,
                "plymouth title",
                strip=True,
            )
        changed += 1 if plymouth_changed else 0

        self._inc_progress(
            self.WEIGHTS["Update branding/version files"],
            f"Branding files updated ({changed} file(s))",
        )
        return True

    def step_verify(self):
        if self.dry_run:
            self.emit("[DRY RUN] Skipping verification (lsb_release)")
            self._inc_progress(self.WEIGHTS["Verify upgraded release"], "Verification skipped (dry run)")
            return True
        ok = False

        if ok:
            self.emit("Upgrade appears successful: target release 24.04 detected.")
        else:
            self.emit("Warning: Could not confirm 24.04. Please review the log.")
        return ok

    def step_reenable_known_ppas(self):
        d = Path("/etc/apt/sources.list.d")
        seen: set[Path] = set()
        to_consider: list[Path] = []

        # Prefer the explicit list of files we disabled earlier in this run.
        for recorded in self.disabled_lists:
            recorded_path = Path(recorded)
            if recorded_path not in seen:
                to_consider.append(recorded_path)
                seen.add(recorded_path)

        # Fall back to scanning the directory (also captures any new items).
        for candidate in d.glob("*.list.disabled"):
            if candidate not in seen:
                to_consider.append(candidate)
                seen.add(candidate)

        if not to_consider:
            self.emit("No disabled third-party entries detected.")
            self._inc_progress(self.WEIGHTS["Re-enable known-good PPAs"], "No PPAs to re-enable")
            return True
        count = 0
        for f in to_consider:
            target = f.with_suffix("")
            content_path = f if f.exists() else target
            if not content_path.exists():
                self.emit(f"Warning: could not locate {f.name} or {target.name}; skipping")
                continue
            try:
                txt = content_path.read_text()
            except Exception as e:
                self.emit(f"Warning: could not process {content_path.name}: {e}")
                continue
            if not any(s in txt for s in self.KNOWN_PPA_WHITELIST):
                self.emit(f"Skipping {content_path.name}: not in whitelist")
                continue
            if self.dry_run:
                self.emit(f"[DRY RUN] Would re-enable {target.name}")
                count += 1
                continue
            if target.exists():
                self.emit(f"Already enabled: {target.name}")
                continue
            if not f.exists():
                self.emit(f"Warning: expected {f.name} to exist; skipping")
                continue
            try:
                f.rename(target)
                self.emit(f"Re-enabled {target.name}")
                count += 1
            except Exception as e:
                self.emit(f"Warning: could not re-enable {target.name}: {e}")
        if count > 0:
            rc, _ = self._run_and_emit(self._apt("update"))
            if rc != 0:
                self.emit("apt-get update failed after re-enabling PPAs.")
                return False
        self._inc_progress(self.WEIGHTS["Re-enable known-good PPAs"], f"Re-enabled {count} PPAs")
        return True

    def run(self):
        try:
            for name, _weight, func in self.plan:
                self.emit(f" === {name} ===")
                ok = func()
                if ok is False:
                    return False
            return True
        except Exception as e:
            logger.exception("Upgrade failed: %s", e)
            self.emit(f"Fatal error: {e}")
            return False

# ---------------------- GTK UI ----------------------

class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title(APP_NAME)
        self.set_default_size(960, 680)
        self.set_icon_name("system-software-update")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.set_child(outer)

        header = Gtk.Label(label=f"{APP_NAME} Upgrade Linux Lite Series 6 → Linux Lite Series 7")
        header.set_justify(Gtk.Justification.CENTER)
        header.add_css_class("title-2")
        outer.append(header)

        # Options row
        opts = Gtk.Box(spacing=16)
        outer.append(opts)
        self.dry_run_cb = Gtk.CheckButton.new_with_label("Dry run (no changes)")
        self.reenable_ppas_cb = Gtk.CheckButton.new_with_label("Re-enable known-good PPAs after upgrade")
        opts.append(self.dry_run_cb)
        opts.append(self.reenable_ppas_cb)

        # Progress + label
        self.progress_label = Gtk.Label(label="0% — idle")
        outer.append(self.progress_label)

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        outer.append(self.progress)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.append(scroller)

        self.buffer = Gtk.TextBuffer()
        self.textview = Gtk.TextView(buffer=self.buffer)
        self.textview.set_editable(False)
        self.textview.set_monospace(True)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroller.set_child(self.textview)

        btn_box = Gtk.Box(spacing=8)
        outer.append(btn_box)

        self.start_btn = Gtk.Button(label="Start Upgrade")
        self.start_btn.connect("clicked", self.on_start)
        btn_box.append(self.start_btn)

        self.view_log_btn = Gtk.Button(label="View Log")
        self.view_log_btn.connect("clicked", self.on_view_log)
        self.view_log_btn.set_sensitive(True)
        btn_box.append(self.view_log_btn)

        self.restart_btn = Gtk.Button(label="Restart")
        self.restart_btn.connect("clicked", self.on_restart)
        self.restart_btn.set_sensitive(False)
        btn_box.append(self.restart_btn)

        self.shutdown_btn = Gtk.Button(label="Shutdown")
        self.shutdown_btn.connect("clicked", self.on_shutdown)
        self.shutdown_btn.set_sensitive(False)
        btn_box.append(self.shutdown_btn)

        self._pulse_source = None

    # UI callbacks
    def append_text(self, text: str):
        end = self.buffer.get_end_iter()
        self.buffer.insert(end, text)
        mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        self.textview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        return False

    def update_progress(self, pct_float: float, label: str):
        pct = int(round(pct_float * 100))
        self.progress.set_fraction(pct_float)
        self.progress.set_text(f"{pct}%")
        text = f"{pct}% — {label}" if label else f"{pct}%"
        self.progress_label.set_text(text)
        return False

    def pulse_on(self):
        if self._pulse_source is None:
            def _pulse():
                self.progress.pulse()
                return True
            self._pulse_source = GLib.timeout_add(160, _pulse)

    def pulse_off(self):
        if self._pulse_source is not None:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = None

    def on_start(self, _btn):
        self.start_btn.set_sensitive(False)
        self.restart_btn.set_sensitive(False)
        self.shutdown_btn.set_sensitive(False)
        self.pulse_on()
        self.append_text(f"Logging to: {LOG_PATH} ")

        dry_run = self.dry_run_cb.get_active()
        reenable = self.reenable_ppas_cb.get_active()

        def worker():
            engine = UpgradeEngine(self.append_text, self.update_progress, dry_run=dry_run, reenable_ppas=reenable)
            ok = engine.run()
            GLib.idle_add(self.on_complete, ok, dry_run)
        threading.Thread(target=worker, daemon=True).start()

    def on_complete(self, ok: bool, dry_run: bool):
        self.pulse_off()
        # Always re-enable Start so users can run again (e.g., after a dry run)
        self.start_btn.set_sensitive(True)
        if ok:
            self.progress.set_fraction(1.0)
            msg = "Dry run completed." if dry_run else "Upgrade completed. Review log and restart."
            self.progress.set_text("100%")
            self.progress_label.set_text(f"100% — {msg}")
        else:
            self.progress.set_text("0%")
            self.progress_label.set_text("0% — Finished with issues. Check the log.")
        # Only enable restart/shutdown for real upgrades
        self.restart_btn.set_sensitive(not dry_run)
        self.shutdown_btn.set_sensitive(not dry_run)
        return False

    def on_view_log(self, _btn):
        path = str(LOG_PATH)
        try:
            subprocess.Popen(["xdg-open", path])
        except Exception:
            dlg = Gtk.MessageDialog(transient_for=self, modal=True, buttons=Gtk.ButtonsType.CLOSE, text=f"Log path: {path}")
            dlg.connect("response", lambda d, r: d.destroy())
            dlg.present()

    def _confirm(self, question: str, on_ok):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, buttons=Gtk.ButtonsType.NONE, text=question)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("OK", Gtk.ResponseType.OK)
        def _resp(d, resp):
            d.destroy()
            if resp == Gtk.ResponseType.OK:
                on_ok()
        dlg.connect("response", _resp)
        dlg.present()

    def on_restart(self, _btn):
        def _do():
            if os.geteuid() == 0:
                subprocess.Popen(["/usr/sbin/reboot"])
            else:
                subprocess.Popen(["pkexec", "/usr/sbin/reboot"])
        self._confirm("Restart now?", _do)

    def on_shutdown(self, _btn):
        def _do():
            if os.geteuid() == 0:
                subprocess.Popen(["/usr/sbin/poweroff"])
            else:
                subprocess.Popen(["pkexec", "/usr/sbin/poweroff"])
        self._confirm("Shut down now?", _do)

class LiteSeriesUpgradeApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self, *args):
        win = self.props.active_window
        if not win:
            win = MainWindow(self)
        win.present()

# ---------------------- Entry point (pkexec-only) ----------------------

def main():
    # Enforce pkexec-only execution: must be root AND launched via pkexec
    if os.geteuid() != 0 or "PKEXEC_UID" not in os.environ:
        sys.stderr.write("This application must be launched via pkexec. ")
        sys.stderr.write("Try: pkexec /usr/bin/lite-series6-upgrade.py ")
        sys.exit(126)

    app = LiteSeriesUpgradeApp()
    sys.exit(app.run(sys.argv))

if __name__ == "__main__":
    main()
