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

"""Build and test Windows wheels for one interpreter or the ARM64 matrix."""

import argparse
import contextlib
import ctypes
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.request
import zipfile
from ctypes import wintypes
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


def install_arm64_python(version):
    install_root = (
        Path(os.environ["LOCALAPPDATA"]) / "crc32c-python" / f"{version}-arm64"
    )
    python = install_root / "python.exe"
    if not python.is_file():
        release = re.sub(r"[a-z].*$", "", version)
        with temporary_directory(prefix="crc32c-python-") as work:
            installer = work / f"python-{version}-arm64.exe"
            print(f"Installing Python {version} ARM64 in {install_root}", flush=True)
            urllib.request.urlretrieve(
                f"https://www.python.org/ftp/python/{release}/{installer.name}",
                installer,
            )
            command = [
                str(installer),
                "/quiet",
                "InstallAllUsers=0",
                "Include_launcher=0",
                "Include_test=0",
                "Include_pip=1",
                "PrependPath=0",
                "Shortcuts=0",
                f"TargetDir={install_root}",
            ]
            result = subprocess.run(command, check=False)
            if result.returncode not in (0, 3010):
                raise subprocess.CalledProcessError(result.returncode, command)
        if not python.is_file():
            raise RuntimeError(
                f"Python {version} ARM64 installer did not create {python}"
            )
    return python


def windows_arm64_host():
    if os.name != "nt":
        return False
    # Environment variables describe the emulated process, not always the host.
    query = ctypes.WinDLL("kernel32", use_last_error=True).IsWow64Process2
    query.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.USHORT),
        ctypes.POINTER(wintypes.USHORT),
    ]
    query.restype = wintypes.BOOL
    process, native = wintypes.USHORT(), wintypes.USHORT()
    if not query(wintypes.HANDLE(-1), ctypes.byref(process), ctypes.byref(native)):
        raise ctypes.WinError(ctypes.get_last_error())
    return native.value == TARGETS["arm64"][2]


def build_arm64_matrix(output_dir, cmake):
    if not windows_arm64_host():
        raise RuntimeError("The ARM64 matrix must run on Windows ARM64 hardware")
    cmake = shutil.which(cmake or "cmake")
    if cmake is None:
        raise RuntimeError("The ARM64 matrix requires CMake on PATH or --cmake")
    config = PACKAGE_ROOT / "scripts" / "python_versions.yaml"
    lines = [
        line for line in config.read_text().splitlines() if line.startswith("versions:")
    ]
    if len(lines) != 1 or not (versions := lines[0].split(":", 1)[1].split()):
        raise ValueError(f"Expected one nonempty versions list in {config}")
    if any(not re.fullmatch(r"3\.\d+\.\d+(?:(?:a|b|rc)\d+)?", v) for v in versions):
        raise ValueError(f"Invalid Python version in {config}")
    # CPython's Windows ARM64 installers start at 3.11.
    versions = [v for v in versions if not v.startswith("3.10.")]
    if not versions:
        raise ValueError(f"No ARM64 Python versions in {config}")
    output_dir = output_dir.resolve()
    scripts = PACKAGE_ROOT / "scripts" / "windows"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    for version in versions:
        python = str(install_arm64_python(version))
        subprocess.run(
            [
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(scripts),
                "-p",
                "test_build_wheel.py",
            ],
            cwd=PACKAGE_ROOT,
            env=env,
            check=True,
        )
        subprocess.run(
            [
                python,
                str(scripts / "build_wheel.py"),
                "--architecture",
                "arm64",
                "--output-dir",
                str(output_dir),
                "--cmake",
                cmake,
            ],
            cwd=PACKAGE_ROOT,
            env=env,
            check=True,
        )
        abi = "cp" + "".join(version.split(".")[:2])
        if not any(output_dir.glob(f"google_crc32c-*-{abi}-{abi}-win_arm64.whl")):
            raise RuntimeError(f"Missing Windows ARM64 wheel for {version}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", required=True, choices=TARGETS)
    parser.add_argument("--output-dir", type=Path, default=PACKAGE_ROOT / "wheels")
    parser.add_argument(
        "--cmake",
        default=shutil.which("cmake"),
        help="CMake executable (default: PATH, then python -m cmake)",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--test-only",
        action="store_true",
        help="Check an existing wheel in --output-dir",
    )
    modes.add_argument(
        "--matrix",
        action="store_true",
        help="Install Python versions and build the ARM64 matrix",
    )
    args = parser.parse_args()
    if args.matrix:
        if args.architecture != "arm64":
            parser.error(
                "--matrix supports arm64 only; use build.bat for the x64 matrix"
            )
        build_arm64_matrix(args.output_dir, args.cmake)
    elif args.test_only:
        validate_interpreter(args.architecture)
        wheel = find_wheel(args.output_dir, args.architecture)
        check_wheel(wheel, args.architecture, args.output_dir.parent)
    else:
        build_wheel(args.architecture, args.output_dir, args.cmake)


if __name__ == "__main__":
    main()
