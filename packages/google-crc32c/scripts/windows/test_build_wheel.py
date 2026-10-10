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

import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import build_wheel


class WheelValidationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def wheel(self, architecture="arm64", dll_machine=0xAA64, pyd_machine=0xAA64):
        abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
        wheel = (
            self.directory / f"google_crc32c-1.9.0-{abi}-{abi}-win_{architecture}.whl"
        )
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, machine in (
                ("google_crc32c/extra-dll/crc32c.dll", dll_machine),
                ("google_crc32c/_crc32c.pyd", pyd_machine),
            ):
                if machine is None:
                    continue
                binary = bytearray(128)
                binary[:2] = b"MZ"
                struct.pack_into("<I", binary, 0x3C, 64)
                struct.pack_into("<4sH", binary, 64, b"PE\0\0", machine)
                archive.writestr(name, binary)
        return wheel

    def test_arm64(self):
        build_wheel.validate_wheel(self.wheel(), "arm64")

    def test_x64(self):
        build_wheel.validate_wheel(self.wheel("amd64", 0x8664, 0x8664), "x64")

    def test_wrong_platform_tag(self):
        with self.assertRaisesRegex(ValueError, "Unexpected wheel tag"):
            build_wheel.validate_wheel(self.wheel("amd64"), "arm64")

    def test_wrong_abi_tag(self):
        wheel = self.wheel()
        wrong_abi = wheel.with_name("google_crc32c-1.9.0-cp39-cp39-win_arm64.whl")
        wheel.rename(wrong_abi)
        with self.assertRaisesRegex(ValueError, "Unexpected wheel tag"):
            build_wheel.validate_wheel(wrong_abi, "arm64")

    def test_missing_dll(self):
        with self.assertRaisesRegex(ValueError, "must contain"):
            build_wheel.validate_wheel(self.wheel(dll_machine=None), "arm64")

    def test_missing_extension(self):
        with self.assertRaisesRegex(ValueError, "must contain"):
            build_wheel.validate_wheel(self.wheel(pyd_machine=None), "arm64")

    def test_x64_dll_in_arm64_wheel(self):
        with self.assertRaisesRegex(ValueError, "not a arm64 PE binary"):
            build_wheel.validate_wheel(self.wheel(dll_machine=0x8664), "arm64")

    def test_x64_extension_in_arm64_wheel(self):
        with self.assertRaisesRegex(ValueError, "not a arm64 PE binary"):
            build_wheel.validate_wheel(self.wheel(pyd_machine=0x8664), "arm64")

    def test_wrong_python_architecture_fails_before_build(self):
        with mock.patch.object(
            build_wheel.sysconfig, "get_platform", return_value="win-amd64"
        ):
            with self.assertRaisesRegex(RuntimeError, "Use native arm64 CPython"):
                build_wheel.build_wheel("arm64", self.directory, "cmake")

    def test_find_wheel_filters_platform_and_abi(self):
        expected = self.wheel()
        self.wheel("amd64", 0x8664, 0x8664)
        (self.directory / "google_crc32c-1.9.0-cp39-cp39-win_arm64.whl").touch()
        self.assertEqual(build_wheel.find_wheel(self.directory, "arm64"), expected)

    def test_find_wheel_requires_exactly_one_match(self):
        with self.assertRaisesRegex(RuntimeError, "Expected one matching wheel"):
            build_wheel.find_wheel(self.directory, "arm64")
        wheel = self.wheel()
        wheel.with_name(wheel.name.replace("1.9.0", "1.8.0")).touch()
        with self.assertRaisesRegex(RuntimeError, "Expected one matching wheel"):
            build_wheel.find_wheel(self.directory, "arm64")

    @mock.patch.object(build_wheel, "validate_interpreter")
    @mock.patch.object(build_wheel.subprocess, "run")
    def test_check_wheel_isolated_and_fail_fast(self, run, validate_interpreter):
        wheel = self.wheel()
        with mock.patch.dict(
            build_wheel.os.environ,
            {"PYTHONPATH": "source", "CRC32C_INSTALL_PREFIX": "build"},
        ):
            build_wheel.check_wheel(wheel, "arm64", self.directory)
        validate_interpreter.assert_called_once_with("arm64")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][:3], [sys.executable, "-m", "venv"])
        self.assertIn(
            ["-m", "pip", "install", "--no-deps", str(wheel)],
            [command[1:] for command in commands],
        )
        self.assertTrue(
            any(
                command[1:3] == ["-m", "pytest"]
                and str(build_wheel.PACKAGE_ROOT / "tests") in command
                for command in commands
            )
        )
        self.assertEqual(commands[-1][1:], ["-m", "twine", "check", str(wheel)])
        for call in run.call_args_list:
            self.assertTrue(call.kwargs["check"])
            self.assertNotEqual(call.kwargs["cwd"], build_wheel.PACKAGE_ROOT)
            self.assertNotIn("PYTHONPATH", call.kwargs["env"])
            self.assertNotIn("CRC32C_INSTALL_PREFIX", call.kwargs["env"])
        run.reset_mock()
        run.side_effect = subprocess.CalledProcessError(1, "venv")
        with self.assertRaises(subprocess.CalledProcessError):
            build_wheel.check_wheel(wheel, "arm64", self.directory)
        run.assert_called_once()

    def test_cli_test_only_does_not_build(self):
        wheel = self.wheel()
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "build_wheel.py",
                    "--architecture",
                    "arm64",
                    "--test-only",
                    "--output-dir",
                    str(self.directory),
                ],
            ),
            mock.patch.object(build_wheel, "validate_interpreter"),
            mock.patch.object(build_wheel, "check_wheel") as check,
            mock.patch.object(build_wheel, "build_wheel") as build,
        ):
            build_wheel.main()
        check.assert_called_once_with(wheel, "arm64", self.directory.parent)
        build.assert_not_called()

    def test_build_uses_target_generator_and_propagates_cmake_failure(self):
        (self.directory / "google_crc32c").mkdir()
        (self.directory / "google_crc32c" / "CMakeLists.txt").touch()
        for architecture, target in [("x64", "x64"), ("arm64", "ARM64")]:
            for cmake in ["custom-cmake", None]:
                with (
                    self.subTest(architecture=architecture, cmake=cmake),
                    mock.patch.object(build_wheel, "PACKAGE_ROOT", self.directory),
                    mock.patch.object(build_wheel, "validate_interpreter"),
                    mock.patch.object(
                        build_wheel.subprocess,
                        "run",
                        side_effect=subprocess.CalledProcessError(1, "cmake"),
                    ) as run,
                    self.assertRaises(subprocess.CalledProcessError),
                ):
                    build_wheel.build_wheel(architecture, self.directory, cmake)
                run.assert_called_once()
                command = run.call_args.args[0]
                prefix = [cmake] if cmake else [sys.executable, "-m", "cmake"]
                self.assertEqual(command[: len(prefix)], prefix)
                self.assertEqual(command[command.index("-A") + 1], target)

    def test_temporary_directory_cleanup_retries_only_file_access_errors(self):
        for winerror, failures in [(5, 2), (32, 1), (267, 1), (5, 5), (87, 1)]:
            error = OSError("cleanup failed")
            error.winerror = winerror
            directory = mock.Mock(name=str(self.directory))
            directory.name = str(self.directory)
            directory.cleanup.side_effect = [error] * failures + [None]
            with (
                self.subTest(winerror=winerror, failures=failures),
                mock.patch.object(
                    build_wheel.tempfile, "TemporaryDirectory", return_value=directory
                ),
                mock.patch.object(build_wheel.time, "sleep") as sleep,
                mock.patch.object(build_wheel.logging, "warning"),
            ):
                if winerror == 87 or failures == 5:
                    with self.assertRaises(OSError):
                        with build_wheel.temporary_directory("test-"):
                            pass
                    self.assertEqual(sleep.call_count, 0 if winerror == 87 else 4)
                else:
                    with build_wheel.temporary_directory("test-") as path:
                        self.assertEqual(path, self.directory)
                    self.assertEqual(directory.cleanup.call_count, failures + 1)
                    self.assertEqual(sleep.call_count, failures)


if __name__ == "__main__":
    unittest.main()
