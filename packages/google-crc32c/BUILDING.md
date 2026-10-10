
# Building

## Be sure to check out all submodules:

```
$ git clone --recursive https://github.com/googleapis/google-cloud-python
$ cd google-cloud-python/packages/google-crc32c
```

## Building and Testing Wheels

The scripts directory contains multiple scripts for building. They are also
used by the CI workflows which can be found at .github/workflows.

On Linux:

```
./scripts/manylinux/build.sh


# Install the wheel that was built as a result
pip install --no-index --find-links=wheels google-crc32c

# Check the package, try and load the native library.
python ./scripts/check_crc32c_extension.py
```

On OS X:

```
# Build the C library and wheel
./scripts/osx/build.sh

# Install the wheel that was built as a result
pip install --no-index --find-links=wheels google-crc32c

# Check the package, try and load the native library.
python ./scripts/check_crc32c_extension.py
```

On Windows:

The shared Windows matrix entry point defaults to x64:

```
.\scripts\windows\build.bat
```

For one interpreter, use a native x64 or ARM64 CPython with pip, Visual Studio
2022 C++ build tools for the target architecture, and CMake on `PATH` (or
installed in that interpreter with `python -m pip install cmake`):

```powershell
# Run from packages\google-crc32c on Windows ARM64 with ARM64 Python.
python .\scripts\windows\build_wheel.py --architecture arm64
# On x64, use --architecture x64 instead.
```

This builds in a temporary directory without resetting or cleaning the
submodule. It verifies the wheel's CPython/platform tags and the PE machine
types of both the extension and bundled DLL. It installs the wheel in a fresh
environment, hides the build-time DLL, and runs the native import check and
package tests before copying the wheel into `wheels`.
Temporary-directory cleanup retries transient Windows file locks up to five
times; persistent cleanup errors fail the command.
Both matrices use this same builder and test implementation. The x64 entry
point retains Chocolatey and the Python launcher for interpreter setup;
`build-arm64.ps1` handles ARM64 interpreter setup and invokes the shared builder.

To retest an existing wheel without rebuilding it:

```powershell
python .\scripts\windows\build_wheel.py --architecture arm64 --test-only
# Existing x64 callers can still use the launcher-based wrapper:
.\scripts\windows\test.bat 3.12
```

`--test-only` requires exactly one wheel matching the interpreter's ABI and
target architecture in `wheels` (or `--output-dir`). It uses a fresh environment,
runs the same binary checks, import check, package tests, and `twine check`, and
does not modify the calling interpreter's installed packages.

To run the entire ARM64 matrix on native Windows ARM64 hardware:

```powershell
$env:CRC32C_ARCH = "arm64"
$env:PUBLISH_WHEELS = "false"
.\scripts\windows\build.bat
```

The ARM64 matrix installs official CPython ARM64 distributions under
`%LOCALAPPDATA%\crc32c-python`, using `scripts/python_versions.yaml`.
Installer cleanup allows up to five attempts, waiting two seconds between
file-access failures; persistent errors stop the matrix.
Windows ARM64 wheels start at Python 3.11 because Python 3.10 has no official
Windows ARM64 installer. The x64 matrix still includes Python 3.10.
The matrix entry point also needs an existing Python launcher (`py -3`) for
distribution validation and publishing, as the x64 release script does.

ARM64 CI worker provisioning and release-job registration are deferred.
The existing Windows release entry point remains unchanged;
`PUBLISH_WHEELS=true` enables publishing, so leave it unset or `false` for local
builds.

The native submodule is temporarily pinned to commit
`000ba0ffe36995ae182ed8385b23fb29c26d6f04` from
[google/crc32c#73](https://github.com/google/crc32c/pull/73).
Before merging/releasing, replace that pin with the upstream merged commit
containing Windows ARM64 support.

## Installing locally for testing

Initialize the submodules and build the main `libcrc32c.so` shared
library using `cmake` / `make`:

```bash
$ cd google-cloud-python/packages/google-crc32c
$ git submodule update --init --recursive
$ python -m venv venv
$ venv/bin/pip install --upgrade setuptools pip wheel
$ venv/bin/pip install cmake
$ mkdir usr
$ export CRC32C_INSTALL_PREFIX=$(pwd)/usr
$ mkdir google_crc32c/build
$ cd google_crc32c/build
$ ../../venv/bin/cmake \
>   -DCMAKE_BUILD_TYPE=Release \
>   -DCRC32C_BUILD_TESTS=no \
>   -DCRC32C_BUILD_BENCHMARKS=no \
>   -DBUILD_SHARED_LIBS=yes \
>   -DCMAKE_INSTALL_PREFIX:PATH=${CRC32C_INSTALL_PREFIX} \
>   ..
$ make all install
$ cd ../..
```

Now, run the tests:

```bash
$ venv/bin/python setup.py build_ext \
    --include-dirs=$(pwd)/usr/include \
    --library-dirs=$(pwd)/usr/lib \
    --rpath=$(pwd)/usr/lib
$ venv/bin/pip install -e .[testing]
$ venv/bin/py.test tests/
============================= test session starts ==============================
platform linux -- Python 3.6.7, pytest-3.10.0, py-1.7.0, pluggy-0.8.0
rootdir: ..., inifile:
collected 9 items

tests/test___init__.py .........                                         [100%]

=========================== 9 passed in 0.03 seconds ===========================
```
