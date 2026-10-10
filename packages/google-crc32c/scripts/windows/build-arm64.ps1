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

$ErrorActionPreference = "Stop"
if ($env:PROCESSOR_ARCHITECTURE -ne "ARM64" -and $env:PROCESSOR_ARCHITEW6432 -ne "ARM64") {
    throw "The ARM64 release matrix must run on Windows ARM64 hardware."
}
Get-Command cmake -ErrorAction Stop | Out-Null
$packageRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$versionsLine = Get-Content "$PSScriptRoot\..\python_versions.yaml" |
    Where-Object { $_ -match '^versions:' }
if (@($versionsLine).Count -ne 1 -or $versionsLine -notmatch '^versions:\s+\S') {
    throw "Expected one nonempty versions list in scripts\python_versions.yaml."
}
$versions = ($versionsLine -replace '^versions:\s*', '').Trim() -split '\s+'

foreach ($version in $versions) {
    # CPython's Windows ARM64 installers start at 3.11.
    if ($version -match '^3\.10\.') {
        continue
    }
    $release = $version -replace '[a-z].*$', ''
    $installRoot = Join-Path $env:LOCALAPPDATA "crc32c-python\$version-arm64"
    $python = Join-Path $installRoot "python.exe"
    if (-not (Test-Path $python)) {
        $installer = Join-Path $env:TEMP "crc32c-python-$version-arm64.exe"
        Invoke-WebRequest "https://www.python.org/ftp/python/$release/python-$version-arm64.exe" -OutFile $installer
        $process = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
            "/quiet", "InstallAllUsers=0", "Include_launcher=0",
            "Include_test=0", "Include_pip=1", "PrependPath=0", "Shortcuts=0",
            "TargetDir=`"$installRoot`""
        )
        if ($process.ExitCode -notin @(0, 3010)) {
            throw "Python $version ARM64 installer failed: $($process.ExitCode)"
        }
        for ($attempt = 1; $attempt -le 5; $attempt++) {
            try {
                Remove-Item -LiteralPath $installer -ErrorAction Stop
                break
            }
            catch [System.UnauthorizedAccessException], [System.IO.IOException] {
                if ($attempt -eq 5) {
                    throw
                }
                Write-Warning "Installer cleanup failed; retrying in 2 seconds: $_"
                Start-Sleep -Seconds 2
            }
        }
    }
    & $python "$PSScriptRoot\build_wheel.py" --architecture arm64
    if ($LASTEXITCODE -ne 0) {
        throw "Python $version ARM64 wheel build failed: $LASTEXITCODE"
    }
    $abi = "cp" + (($version -split '\.')[0..1] -join '')
    if (-not (Test-Path "$packageRoot\wheels\google_crc32c-*-$abi-$abi-win_arm64.whl")) {
        throw "Missing Windows ARM64 wheel for $version"
    }
}
