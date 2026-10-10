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

    def test_host_detection_uses_native_architecture(self):
        with mock.patch.object(build_wheel.ctypes, "WinDLL") as dll:
            query = dll.return_value.IsWow64Process2
            for machine in (0xAA64, 0x8664):

                def result(handle, process, native):
                    native._obj.value = machine
                    return True

                query.side_effect = result
                self.assertEqual(build_wheel.windows_arm64_host(), machine == 0xAA64)
            query.side_effect = None
            query.return_value = False
            with self.assertRaises(OSError):
                build_wheel.windows_arm64_host()


class Arm64MatrixTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.scripts = self.root / "scripts" / "windows"
        self.scripts.mkdir(parents=True)
        self.config = self.scripts.parent / "python_versions.yaml"
        self.config.write_text("versions: 3.10.11 3.11.9 3.15.0rc2\n")
        self.output = self.root / "wheels"
        self.output.mkdir()
        for patch in (
            mock.patch.object(build_wheel, "PACKAGE_ROOT", self.root),
            mock.patch.object(build_wheel, "windows_arm64_host", return_value=True),
            mock.patch.dict(
                build_wheel.os.environ,
                {
                    "LOCALAPPDATA": str(self.root),
                },
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def python(self, version):
        return self.root / "crc32c-python" / f"{version}-arm64" / "python.exe"

    @mock.patch.object(build_wheel.subprocess, "run")
    @mock.patch.object(build_wheel.urllib.request, "urlretrieve")
    def test_installer_reuses_existing_interpreter(self, download, run):
        python = self.python("3.11.9")
        python.parent.mkdir(parents=True)
        python.touch()
        self.assertEqual(build_wheel.install_arm64_python("3.11.9"), python)
        download.assert_not_called()
        run.assert_not_called()

    @mock.patch.object(build_wheel.urllib.request, "urlretrieve")
    def test_installer_prerelease_and_exit_codes(self, download):
        for code in (0, 3010, 1):
            with self.subTest(code=code):
                python = self.python("3.15.0rc2")

                def install(command, check):
                    self.assertFalse(check)
                    self.assertIn(f"TargetDir={python.parent}", command)
                    self.assertIn("Include_launcher=0", command)
                    if code in (0, 3010):
                        python.parent.mkdir(parents=True, exist_ok=True)
                        python.touch()
                    return subprocess.CompletedProcess(command, code)

                with mock.patch.object(
                    build_wheel.subprocess, "run", side_effect=install
                ):
                    if code == 1:
                        with self.assertRaises(subprocess.CalledProcessError):
                            build_wheel.install_arm64_python("3.15.0rc2")
                    else:
                        self.assertEqual(
                            build_wheel.install_arm64_python("3.15.0rc2"), python
                        )
                        python.unlink()
                self.assertEqual(
                    download.call_args.args[0],
                    "https://www.python.org/ftp/python/3.15.0/python-3.15.0rc2-arm64.exe",
                )

    @mock.patch.object(build_wheel.urllib.request, "urlretrieve")
    @mock.patch.object(build_wheel.subprocess, "run")
    def test_installer_rejects_success_without_interpreter(self, run, download):
        run.return_value.returncode = 0
        with self.assertRaisesRegex(RuntimeError, "did not create"):
            build_wheel.install_arm64_python("3.11.9")

    @mock.patch.object(build_wheel.shutil, "which", return_value="cmake.exe")
    @mock.patch.object(build_wheel, "install_arm64_python")
    def test_matrix_uses_each_target_interpreter_after_tests(self, install, which):
        install.side_effect = self.python

        def run(command, **kwargs):
            self.assertTrue(kwargs["check"])
            self.assertEqual(kwargs["cwd"], self.root)
            if "--architecture" in command:
                self.assertNotIn("--matrix", command)
                self.assertIn(str(self.output.resolve()), command)
                self.assertEqual(command[-2:], ["--cmake", "cmake.exe"])
                version = Path(command[0]).parent.name.removesuffix("-arm64")
                abi = "cp" + "".join(version.split(".")[:2])
                (self.output / f"google_crc32c-1.9.0-{abi}-{abi}-win_arm64.whl").touch()

        with mock.patch.object(
            build_wheel.subprocess, "run", side_effect=run
        ) as runner:
            build_wheel.build_arm64_matrix(self.output, "custom-cmake")
        which.assert_called_once_with("custom-cmake")
        self.assertEqual(
            install.call_args_list, [mock.call("3.11.9"), mock.call("3.15.0rc2")]
        )
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(len(commands), 4)
        for i, version in [(0, "3.11.9"), (2, "3.15.0rc2")]:
            self.assertEqual(
                commands[i],
                [
                    str(self.python(version)),
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(self.scripts),
                    "-p",
                    "test_build_wheel.py",
                ],
            )
            self.assertEqual(commands[i + 1][0], commands[i][0])

    @mock.patch.object(build_wheel.shutil, "which", return_value="cmake.exe")
    @mock.patch.object(build_wheel, "install_arm64_python")
    def test_matrix_stops_on_failed_test_build_or_missing_wheel(self, install, which):
        install.side_effect = self.python
        for failed_step in (0, 1, 2):
            with self.subTest(failed_step=failed_step):
                effects = [None] * failed_step
                if failed_step < 2:
                    effects.append(subprocess.CalledProcessError(23, "python"))
                with mock.patch.object(
                    build_wheel.subprocess, "run", side_effect=effects
                ) as run:
                    error = (
                        subprocess.CalledProcessError
                        if failed_step < 2
                        else RuntimeError
                    )
                    with self.assertRaises(error):
                        build_wheel.build_arm64_matrix(self.output, None)
                    self.assertEqual(run.call_count, min(failed_step + 1, 2))

    @mock.patch.object(build_wheel.shutil, "which", return_value="cmake.exe")
    @mock.patch.object(build_wheel, "install_arm64_python")
    def test_matrix_rejects_invalid_versions_before_install(self, install, which):
        for text in (
            "",
            "versions:\n",
            "versions: 3.11.9\nversions: 3.12.10",
            "versions: ../3.11.9",
            "versions: 3.10.11",
        ):
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaises(ValueError):
                    build_wheel.build_arm64_matrix(self.output, None)
        install.assert_not_called()

    def test_matrix_requires_arm64_host_and_cmake(self):
        with mock.patch.object(build_wheel, "windows_arm64_host", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Windows ARM64 hardware"):
                build_wheel.build_arm64_matrix(self.output, None)
        with mock.patch.object(build_wheel.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "requires CMake"):
                build_wheel.build_arm64_matrix(self.output, None)

    def test_cli_matrix_routing_and_incompatible_modes(self):
        with (
            mock.patch.object(
                sys, "argv", ["build_wheel.py", "--architecture", "arm64", "--matrix"]
            ),
            mock.patch.object(build_wheel, "build_arm64_matrix") as matrix,
            mock.patch.object(build_wheel, "build_wheel") as build,
        ):
            build_wheel.main()
        matrix.assert_called_once()
        build.assert_not_called()
        for args in (
            ["--architecture", "x64", "--matrix"],
            ["--architecture", "arm64", "--matrix", "--test-only"],
        ):
            with (
                self.subTest(args=args),
                mock.patch.object(sys, "argv", ["build_wheel.py", *args]),
                mock.patch.object(sys, "stderr"),
                self.assertRaises(SystemExit) as error,
            ):
                build_wheel.main()
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
