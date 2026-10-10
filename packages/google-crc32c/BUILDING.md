
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

Use CPython with pip and Visual Studio 2022 C++ build tools matching the target
architecture. CMake must be on `PATH`, or installed in the interpreter with
`python -m pip install cmake` for single-interpreter builds.

```powershell
# Run from packages\google-crc32c with ARM64 Python on Windows ARM64.
python .\scripts\windows\build_wheel.py --architecture arm64
# With x64 Python, use --architecture x64 instead.
```

The command builds and tests the wheel, then writes it to `wheels`.
To retest an existing wheel matching the interpreter and architecture:

```powershell
python .\scripts\windows\build_wheel.py --architecture arm64 --test-only
# Existing x64 test wrapper:
.\scripts\windows\test.bat 3.12
```

To build all versions in `scripts/python_versions.yaml`, use the matrix entry
point (x64 by default):

```powershell
$env:PUBLISH_WHEELS = "false"
$env:CRC32C_ARCH = "x64" # Use "arm64" on Windows ARM64.
.\scripts\windows\build.bat
```

Both matrices require the Python launcher (`py -3`) and run the builder's
regression tests before each wheel build. The x64 matrix uses Chocolatey;
the ARM64 matrix uses a bootstrap Python 3.10 or newer, requires CMake on `PATH`,
and installs Python under
`%LOCALAPPDATA%\crc32c-python`. ARM64 starts at Python 3.11; x64 includes 3.10.
Keep `PUBLISH_WHEELS=false` for local builds; `true` enables publishing.

To run only the ARM64 wheel matrix without the batch release steps:

```powershell
python .\scripts\windows\build_wheel.py --architecture arm64 --matrix
```

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
