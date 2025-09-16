diff --git a/README.md b/README.md
index e53e4a2836bb198ec1bb9e05273aa7b591280cd6..f94cfbe2bcb297af2e4e95fae8f456c2305b83b0 100644
--- a/README.md
+++ b/README.md
@@ -1 +1,58 @@
-# lite-series-upgrade
+# Lite Series Upgrade
+
+GTK4-based desktop utility that walks Linux Lite 6.x users through an
+automated in-place upgrade to the Linux Lite 7.x series. The application
+exposes a simple interface for tracking progress, watching log output, and
+toggling a dry-run or optional PPA re-enable step.
+
+## Features
+
+- Enforces execution via `pkexec` so that privileged operations are always
+  launched with authentication prompts.
+- Tracks high-level upgrade stages with weighted progress for accurate
+  percentage reporting.
+- Provides a dry-run mode that short-circuits expensive APT and DPKG
+  operations, allowing administrators to evaluate the plan instantly.
+- Automatically fixes common package issues, disables third-party sources,
+  and re-enables a vetted PPA list on demand.
+- Downloads and installs the LibreOffice Series 7 bundle directly from the
+  Linux Lite upgrade repository.
+- Logs to `/var/log/ll-series-upgrade.log` (falling back to `/tmp` when
+  running unprivileged) and offers quick access through the UI.
+- Offers convenience actions to restart or shut down after a successful
+  upgrade.
+
+## Runtime Requirements
+
+The program targets Linux Lite 6.x systems and expects the Ubuntu 22.04
+package base. Install the GTK 4 and Python bindings along with standard
+system tools before launching:
+
+```bash
+sudo apt install python3-gi gir1.2-gtk-4.0 \
+    update-manager-core ubuntu-release-upgrader-core
+```
+
+> **Note:** The script must be installed as `/usr/bin/lite-series6-upgrade.py`
+> (or symlinked accordingly) and executed via `pkexec` so that it runs with
+> administrative privileges.
+
+## Usage
+
+```bash
+pkexec /usr/bin/lite-series6-upgrade.py
+```
+
+Choose between a real upgrade or a dry run using the checkboxes. The log view
+updates live; a full log is also available at the path shown in the window.
+
+## Development Tips
+
+- Run a syntax check before committing changes:
+  ```bash
+  python3 -m py_compile lite-series6-upgrade.py
+  ```
+- GTK can be exercised on a development workstation running Linux Lite or
+  any Ubuntu 22.04/24.04 environment with the required packages installed.
+
+Pull requests and improvements are welcome!
