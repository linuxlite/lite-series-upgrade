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

# GTK
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gio

APP_NAME = "Lite Series 6 Upgrade"
APP_ID = "com.linuxlite.LiteSeries6Upgrade"
LL_FROM_VERSION = "6.6"
LL_TO_VERSION = "7.0"
LL_TO_SERIES_LTS = "7.6"
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
                    backup = fpath.with_suffix(fpath.suffix + f".bak-{int(time.time())}")
                    if self.dry_run:
                        self.emit(f"[DRY RUN] Would back up {fname} -> {backup.name} and replace 'fluorite' with 'galena'")
                    else:
                        shutil.copy2(fpath, backup)
                        new_content = content.replace("fluorite", "galena")
                        fpath.write_text(new_content)
                        self.emit(f"Updated {fname}: fluorite -> galena (backup: {backup.name})")
                else:
                    self.emit(f"No 'fluorite' found in {fname}; no change needed")
            except Exception as e:
                self.emit(f"Warning: could not update {fname}: {e}")

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
        pkgs = ("update-manager-core", "ubuntu-release-upgrader-core")
        weight = self.WEIGHTS["Ensure upgrade tools"]
        per = max(1, weight // len(pkgs))
        last_bonus = weight - per * (len(pkgs) - 1)
        for i, pkg in enumerate(pkgs):
            rc, _ = self._run_and_emit(self._apt("install", pkg))
            if rc != 0:
                self.emit(f"Failed to install {pkg} (rc={rc}).")
                return False
            self._inc_progress(last_bonus if i == len(pkgs) - 1 else per, f"Ensured {pkg}")
        return True

    def step_release_upgrade(self):
        # Ensure LTS prompt
        try:
            conf = Path("/etc/update-manager/release-upgrades")
            if conf.exists() and not self.dry_run:
                bak = conf.with_suffix(".bak")
                shutil.copy2(conf, bak)
            if not self.dry_run:
                conf.write_text("""[DEFAULT]
Prompt=lts
""")
            else:
                self.emit("[DRY RUN] Would set Prompt=lts in /etc/update-manager/release-upgrades")
        except Exception as e:
            self.emit(f"Warning: could not write release-upgrades config: {e}")
        prep_weight = 1
        self._inc_progress(prep_weight, "Prepared release-upgrades config")

        cmd = ["/usr/bin/do-release-upgrade", "-f", "DistUpgradeViewNonInteractive"]
        remaining = self.WEIGHTS["Release upgrade to 24.04"] - prep_weight
        if remaining < 1:
            remaining = 1
        if self.dry_run:
            self.emit("[DRY RUN] Would launch: do-release-upgrade -f DistUpgradeViewNonInteractive")
            self._inc_progress(remaining, "Simulated release upgrade")
            return True
        rc, _ = self._run_and_emit(cmd, dry_run=False)
        if rc != 0:
            self.emit(f"Release upgrade command failed (rc={rc}).")
            return False
        self._inc_progress(remaining, "Release upgrade complete")
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
                backup = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
                shutil.copy2(path, backup)
                path.write_text(new_content)
                self.emit(f"Updated {path} (backup: {backup.name})")
                return True
            else:
                self.emit(f"No changes needed in {path}")
                return False
        except Exception as e:
            self.emit(f"Warning: could not update {path}: {e}")
            return False

    def step_update_branding(self):
        self.emit("Updating Linux Lite branding/version files…")
        changed = 0
        changed += bool(self._replace_in_file(Path("/etc/llver"), [(f"Linux Lite {LL_FROM_VERSION}", f"Linux Lite {LL_TO_VERSION}")]))
        changed += bool(self._replace_in_file(Path("/etc/issue"), [(f"Linux Lite {LL_FROM_VERSION} LTS \n \l", f"Linux Lite {LL_TO_SERIES_LTS} LTS \n \l")]))
        changed += bool(self._replace_in_file(Path("/etc/lsb-release"), [(f'DISTRIB_DESCRIPTION="Linux Lite {LL_FROM_VERSION}"', f'DISTRIB_DESCRIPTION="Linux Lite {LL_TO_VERSION}"')]))
        changed += bool(self._replace_in_file(Path("/etc/os-release"), [(f'PRETTY_NAME="Linux Lite {LL_FROM_VERSION}"', f'PRETTY_NAME="Linux Lite {LL_TO_VERSION}"')]))
        changed += bool(self._replace_in_file(Path("/usr/share/plymouth/themes/text.plymouth"), [(f"title=Linux Lite {LL_FROM_VERSION}", f"title=Linux Lite {LL_TO_VERSION}")]))
        self._inc_progress(self.WEIGHTS["Update branding/version files"], f"Branding files updated ({changed} file(s))")
        return True

    def step_verify(self):
        if self.dry_run:
            self.emit("[DRY RUN] Skipping verification (lsb_release)")
            self.emit("[DRY RUN] Would update /etc/llver, /etc/issue, /etc/lsb-release, /etc/os-release, and text.plymouth with new version strings")
            self._inc_progress(self.WEIGHTS["Verify upgraded release"], "Verification & branding skipped (dry run)")
            return True
        ok = False
                ok = True
            else:
                if codename_raw:
                    self.emit(f"Note: lsb_release -cs returned {codename_raw} (expected noble).")
                else:
                    self.emit("Warning: lsb_release -cs produced no output.")
        # Update version branding files
        try:
            Path("/etc/llver").write_text("Linux Lite 7.0")
            self.emit("Updated /etc/llver → Linux Lite 7.0")
        except Exception as e:
            self.emit(f"Warning: could not update /etc/llver: {e}")
        try:
            Path("/etc/issue").write_text("Linux Lite 7.6 LTS \n \l")
            self.emit("Updated /etc/issue → Linux Lite 7.6 LTS")
        except Exception as e:
            self.emit(f"Warning: could not update /etc/issue: {e}")
        try:
            lsb = Path("/etc/lsb-release").read_text().splitlines()
            new_lsb = []
            for line in lsb:
                if line.startswith("DISTRIB_DESCRIPTION="):
                    new_lsb.append('DISTRIB_DESCRIPTION="Linux Lite 7.0"')
                else:
                    new_lsb.append(line)
            Path("/etc/lsb-release").write_text("\n".join(new_lsb) + "\n")
            self.emit("Updated /etc/lsb-release → DISTRIB_DESCRIPTION=Linux Lite 7.0")
        except Exception as e:
            self.emit(f"Warning: could not update /etc/lsb-release: {e}")
        try:
            osrel = Path("/etc/os-release").read_text().splitlines()
            new_osrel = []
            for line in osrel:
                if line.startswith("PRETTY_NAME="):
                    new_osrel.append('PRETTY_NAME="Linux Lite 7.0"')
                else:
                    new_osrel.append(line)
            Path("/etc/os-release").write_text("\n".join(new_osrel) + "\n")
            self.emit("Updated /etc/os-release → PRETTY_NAME=Linux Lite 7.0")
        except Exception as e:
            self.emit(f"Warning: could not update /etc/os-release: {e}")
        try:
            ply = Path("/usr/share/plymouth/themes/text.plymouth").read_text().splitlines()
            new_ply = []
            for line in ply:
                if line.strip().startswith("title="):
                    new_ply.append("title=Linux Lite 7.0")
                else:
                    new_ply.append(line)
            Path("/usr/share/plymouth/themes/text.plymouth").write_text("\n".join(new_ply) + "\n")
            self.emit("Updated plymouth text theme title → Linux Lite 7.0")
        except Exception as e:
            self.emit(f"Warning: could not update plymouth theme: {e}")

        self._inc_progress(self.WEIGHTS["Verify upgraded release"], "Verification & branding complete")
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
        outer.append(scroller)

        self.buffer = Gtk.TextBuffer()
        self.textview = Gtk.TextView(buffer=self.buffer)
        self.textview.set_editable(False)
        self.textview.set_monospace(True)
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
