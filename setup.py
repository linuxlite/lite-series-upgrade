from __future__ import annotations

from pathlib import Path

from setuptools import find_packages, setup

ROOT = Path(__file__).parent.resolve()
PACKAGE_NAME = "lite_series_upgrade"
PACKAGE_DIR = ROOT / PACKAGE_NAME

PACKAGE_FILES = [
    str(path.relative_to(ROOT))
    for path in PACKAGE_DIR.rglob("*")
    if path.is_file() and path.suffix == ".py"
]

setup(
    name="lite-series-upgrade",
    version="0.1.0",
    description="GTK4-based desktop utility for upgrading Linux Lite 6.x systems to the 7.x series.",
    long_description=(ROOT / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    license="GPL-2.0-only",
    license_files=["LICENSE"],
    python_requires=">=3.10",
    packages=find_packages(where="."),
    include_package_data=True,
    zip_safe=False,
    scripts=["lite-series6-upgrade.py"],
    data_files=[
        (f"lib/lite-series-upgrade/{PACKAGE_NAME}", PACKAGE_FILES),
    ],
)
