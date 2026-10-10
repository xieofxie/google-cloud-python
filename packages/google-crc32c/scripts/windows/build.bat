@rem Copyright 2019 Google LLC. All rights reserved.
@rem
@rem Licensed under the Apache License, Version 2.0 (the "License");
@rem you may not use this file except in compliance with the License.
@rem You may obtain a copy of the License at
@rem
@rem     http://www.apache.org/licenses/LICENSE-2.0
@rem
@rem Unless required by applicable law or agreed to in writing, software
@rem distributed under the License is distributed on an "AS IS" BASIS,
@rem WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
@rem See the License for the specific language governing permissions and
@rem limitations under the License.


setlocal ENABLEDELAYEDEXPANSION

cd /d "%~dp0..\.." || goto :error

set CRC32C_PURE_PYTHON=0
if "%CRC32C_ARCH%"=="" set CRC32C_ARCH=x64
if not "%CRC32C_ARCH%"=="x64" if not "%CRC32C_ARCH%"=="arm64" (
    echo "Unsupported CRC32C_ARCH: %CRC32C_ARCH%. Expected x64 or arm64."
    goto :error
)
if not exist google_crc32c\CMakeLists.txt (
    git submodule update --init --recursive || goto :error
)
if "%CRC32C_ARCH%"=="arm64" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-arm64.ps1" || goto :error
    goto :validate
)

@rem Iterate through supported Python versions.
@rem Keep the existing Chocolatey/launcher setup for x64.
for /f "tokens=2 delims=:" %%A in ('findstr /b "versions:" "%~dp0..\python_versions.yaml"') do set SUPPORTED_PYTHON_VERSIONS=%%A
@rem Set PY_PYTHON3 to the first (lowest supported) version in python_versions.yaml so py -3 uses a stable version.
for /f "tokens=1 delims= " %%V in ("%SUPPORTED_PYTHON_VERSIONS%") do for /f "tokens=1,2 delims=." %%I in ("%%V") do set PY_PYTHON3=%%I.%%J
FOR %%P IN (%SUPPORTED_PYTHON_VERSIONS%) DO (
    set python_version=%%P
    for /f "tokens=1,2 delims=." %%I in ("%%P") do set python_version_trimmed=%%I.%%J

    for /L %%R in (1,1,5) do (
        py -!python_version_trimmed!-64 --version >nul 2>&1 || (
            echo "Installing Python version %%P (attempt %%R/5)"
            @rem Format pre-release version for Chocolatey (e.g. 3.15.0a1 -> 3.15.0-a1)
            set CHOCO_VER=%%P
            set CHOCO_VER=!CHOCO_VER:0a=0-a!
            set CHOCO_VER=!CHOCO_VER:0b=0-b!
            set CHOCO_VER=!CHOCO_VER:0rc=0-rc!
            choco install python --version=!CHOCO_VER! --pre -y --no-progress || (
                echo "choco install %%P failed; retrying in 15s..."
                py -3 -c "import time; time.sleep(15)"
            )
        )
    )
    py -!python_version_trimmed!-64 --version >nul 2>&1 || goto :error

    echo "Listing available Python versions"
    py -0

    py -!python_version_trimmed!-64 -m pip install --upgrade pip || goto :error

    echo "Installing cmake for Python %%P"
    py -!python_version_trimmed!-64 -m pip install cmake || goto :error

    py -!python_version_trimmed!-64 "%~dp0build_wheel.py" --architecture x64 || goto :error
    echo "Finished with Python version %%P"
)

echo "Built wheels in wheels/ directory:"
dir wheels\*.whl || goto :error

for %%P in (%SUPPORTED_PYTHON_VERSIONS%) do (
    for /f "tokens=1,2 delims=." %%I in ("%%P") do set py_tag=cp%%I%%J
    dir wheels\google_crc32c-*-!py_tag!-!py_tag!-win_amd64.whl >nul || (
        echo "ERROR: Missing Windows wheel for !py_tag!!"
        goto :error
    )
)

:validate
echo "Validating built wheels with twine check"
py -3 -m pip install --upgrade twine wheel setuptools pkginfo || goto :error

echo "Build the source distribution (sdist) and validate with twine check"
py -3 setup.py sdist || goto :error
py -3 -m twine check dist/* wheels/* || goto :error

if "%PUBLISH_WHEELS%"=="true" (
    echo "Start the releasetool reporter"
    py -3 -m pip install gcp-releasetool || goto :error
    if not exist C:\temp mkdir C:\temp
    py -3 -m releasetool publish-reporter-script > C:\temp\publisher-script || goto :error

    echo "Disable buffering, so that the logs stream through."
    set PYTHONUNBUFFERED=1

    echo "## RELEASE WORKFLOW SUCCESSFUL ##"
    echo "## Uploading Wheels and sdist ##"

    set /p TWINE_PASSWORD=<%KOKORO_KEYSTORE_DIR%/73713_google-cloud-pypi-token-keystore-3
    py -3 -m twine upload --skip-existing --username __token__ --password "!TWINE_PASSWORD!" dist/* wheels/* || goto :error
    dir wheels
    dir dist
) else (
    echo "PUBLISH_WHEELS is '%PUBLISH_WHEELS%'. Skipping PyPI upload after sdist and wheel validation."
)

goto :EOF

:error
exit /b 1
