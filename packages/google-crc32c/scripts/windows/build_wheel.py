# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Build and exercise one Windows wheel using the current CPython interpreter."""

import argparse
import contextlib
import logging
import os
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile
import time
import zipfile
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
TARGETS = {
    "x64": ("win-amd64", "win_amd64", 0x8664),
    "arm64": ("win-arm64", "win_arm64", 0xAA64),
}


@contextlib.contextmanager
def temporary_directory(prefix):
    directory = tempfile.TemporaryDirectory(prefix=prefix)
    try:
        yield Path(directory.name)
    finally:
        for attempt in range(5):
            try:
                directory.cleanup()
                break
            except OSError as error:
                # Older CPython can report a locked .pyd as "not a directory".
                if getattr(error, "winerror", None) not in (5, 32, 267) or attempt == 4:
                    raise
                logging.warning(
                    "Temporary directory cleanup failed; retrying: %s", error
                )
                time.sleep(2)


def validate_wheel(wheel, architecture):
    """Reject incorrectly tagged wheels, missing DLLs, and mixed architectures."""
    _, wheel_platform, machine = TARGETS[architecture]
    abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if not wheel.name.endswith(f"-{abi}-{abi}-{wheel_platform}.whl"):
        raise ValueError(f"Unexpected wheel tag: {wheel.name}")
    with zipfile.ZipFile(wheel) as archive:
        dll = "google_crc32c/extra-dll/crc32c.dll"
        extensions = [
            name
            for name in archive.namelist()
            if name.startswith("google_crc32c/_crc32c") and name.endswith(".pyd")
        ]
        if dll not in archive.namelist() or len(extensions) != 1:
            raise ValueError(f"Wheel must contain one CRC32C extension and {dll}")
        for name in [dll, *extensions]:
            binary = archive.read(name)
            if binary[:2] != b"MZ":
                raise ValueError(f"{name} is not a PE binary")
            pe_offset = struct.unpack_from("<I", binary, 0x3C)[0]
            signature, actual_machine = struct.unpack_from("<4sH", binary, pe_offset)
            if signature != b"PE\0\0" or actual_machine != machine:
                raise ValueError(f"{name} is not a {architecture} PE binary")


def validate_interpreter(architecture):
    python_platform, _, _ = TARGETS[architecture]
    if os.name != "nt" or sysconfig.get_platform() != python_platform:
        raise RuntimeError(
            f"Use native {architecture} CPython, not {sysconfig.get_platform()}"
        )
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        raise RuntimeError("Free-threaded Python wheels are not supported")


def check_wheel(wheel, architecture, report_dir):
    """Exercise an installed wheel without using the source or build-time DLL."""
    validate_interpreter(architecture)
    validate_wheel(wheel, architecture)
    wheel = wheel.resolve()
    report_dir = report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CRC32C_PURE_PYTHON"] = "0"
    env.pop("PYTHONPATH", None)
    env.pop("CRC32C_INSTALL_PREFIX", None)
    with temporary_directory(prefix=f"crc32c-test-{architecture}-") as work:

        def run(*command):
            subprocess.run([str(arg) for arg in command], cwd=work, env=env, check=True)

        run(sys.executable, "-m", "venv", work / "test-env")
        python = work / "test-env" / "Scripts" / "python.exe"
        run(python, "-m", "pip", "install", "--no-deps", wheel)
        run(python, "-m", "pip", "install", "pytest", "twine")
        run(python, PACKAGE_ROOT / "scripts" / "check_crc32c_extension.py")
        run(
            python,
            "-m",
            "pytest",
            PACKAGE_ROOT / "tests",
            f"--junitxml={report_dir / (wheel.stem + '_sponge_log.xml')}",
        )
        run(python, "-m", "twine", "check", wheel)


def find_wheel(directory, architecture):
    _, wheel_platform, _ = TARGETS[architecture]
    abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    wheels = list(directory.glob(f"google_crc32c-*-{abi}-{abi}-{wheel_platform}.whl"))
    if len(wheels) != 1:
        raise RuntimeError(
            f"Expected one matching wheel in {directory}, found {wheels}"
        )
    return wheels[0]


def build_wheel(architecture, output_dir, cmake):
    validate_interpreter(architecture)
    native_source = PACKAGE_ROOT / "google_crc32c"
    if not (native_source / "CMakeLists.txt").is_file():
        raise RuntimeError("Initialize the google_crc32c Git submodule before building")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CRC32C_PURE_PYTHON"] = "0"
    env.pop("PYTHONPATH", None)
    cmake_command = [cmake] if cmake else [sys.executable, "-m", "cmake"]

    with temporary_directory(prefix=f"crc32c-{architecture}-") as work:
        prefix = work / "install"
        env["CRC32C_INSTALL_PREFIX"] = str(prefix)

        def run(*command):
            subprocess.run([str(arg) for arg in command], cwd=work, env=env, check=True)

        run(
            *cmake_command,
            "-S",
            native_source,
            "-B",
            work / "native",
            "-G",
            "Visual Studio 17 2022",
            "-A",
            "ARM64" if architecture == "arm64" else "x64",
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.12",
            "-DCRC32C_BUILD_BENCHMARKS=no",
            "-DCRC32C_BUILD_TESTS=no",
            "-DBUILD_SHARED_LIBS=yes",
            "-DCMAKE_WINDOWS_EXPORT_ALL_SYMBOLS=yes",
            "-DCRC32C_USE_GLOG=0",
            f"-DCMAKE_INSTALL_PREFIX:PATH={prefix}",
        )
        run(
            *cmake_command,
            "--build",
            work / "native",
            "--config",
            "RelWithDebInfo",
            "--target",
            "install",
        )

        # Stage only packaging inputs, never stale build outputs from another ABI.
        source = work / "source"
        shutil.copytree(
            PACKAGE_ROOT / "src",
            source / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyd", "*.dll"),
        )
        for name in (
            "setup.py",
            "setup.cfg",
            "pyproject.toml",
            "MANIFEST.in",
            "README.md",
            "LICENSE",
        ):
            shutil.copy2(PACKAGE_ROOT / name, source / name)
        run(
            sys.executable,
            "-m",
            "pip",
            "wheel",
            source,
            "--no-deps",
            "--wheel-dir",
            work / "wheels",
        )
        wheels = list((work / "wheels").glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"Expected one built wheel, found {wheels}")
        wheel = wheels[0]
        validate_wheel(wheel, architecture)

        # Hide the build-time DLL to prove that the installed wheel is standalone.
        prefix.rename(work / "hidden-install")
        check_wheel(wheel, architecture, output_dir.parent)
        shutil.copy2(wheel, output_dir / wheel.name)
        print(f"Built and checked {output_dir / wheel.name}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", required=True, choices=TARGETS)
    parser.add_argument("--output-dir", type=Path, default=PACKAGE_ROOT / "wheels")
    parser.add_argument(
        "--cmake",
        default=shutil.which("cmake"),
        help="CMake executable (default: PATH, then python -m cmake)",
    )
    parser.add_argument(
        "--test-only",
        action="store_true",
        help="Check an existing wheel in --output-dir",
    )
    args = parser.parse_args()
    if args.test_only:
        validate_interpreter(args.architecture)
        wheel = find_wheel(args.output_dir, args.architecture)
        check_wheel(wheel, args.architecture, args.output_dir.parent)
    else:
        build_wheel(args.architecture, args.output_dir, args.cmake)


if __name__ == "__main__":
    main()
