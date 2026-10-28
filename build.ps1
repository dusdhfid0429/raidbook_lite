# RaidBook Lite 빌드 — PyInstaller(exe 3개) + ffmpeg.exe + Inno Setup(설치파일)
# 사용: .\build.ps1 [-SkipInstaller]   (GitHub Actions도 이 스크립트를 그대로 사용)
param([switch]$SkipInstaller)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Version = (python -c "import version; print(version.VERSION)").Trim()
Write-Host "RaidBook Lite v$Version 빌드" -ForegroundColor Cyan

# version_info.txt 버전 주입 (exe 속성)
$parts = ($Version.Split('.') + @('0','0','0','0'))[0..3]
$tuple = $parts -join ', '
$dotted = $parts -join '.'
(Get-Content version_info.txt -Raw -Encoding UTF8) `
    -replace 'filevers=\([^)]*\)', "filevers=($tuple)" `
    -replace 'prodvers=\([^)]*\)', "prodvers=($tuple)" `
    -replace "u'FileVersion', u'[^']*'", "u'FileVersion', u'$dotted'" `
    -replace "u'ProductVersion', u'[^']*'", "u'ProductVersion', u'$dotted'" |
    Set-Content version_info.txt -Encoding UTF8

if (Test-Path dist) { Remove-Item dist -Recurse -Force }
if (Test-Path build) { Remove-Item build -Recurse -Force }

python -m PyInstaller --clean --noconfirm RaidBook.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 실패 ($LASTEXITCODE)" }
# 음악 봇용 FFmpeg — imageio-ffmpeg 패키지에 든 Windows 실행 파일을 그대로 복사
$Ffmpeg = (python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())").Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Ffmpeg)) { throw "ffmpeg 찾기 실패 (pip install imageio-ffmpeg)" }
Copy-Item $Ffmpeg "dist\ffmpeg.exe" -Force
foreach ($exe in @("RaidBook.exe", "RaidBook_Boss_Bot.exe", "RaidBook_Music_Bot.exe", "ffmpeg.exe")) {
    if (-not (Test-Path "dist\$exe")) { throw "dist\$exe 생성 안 됨" }
    Write-Host ("  ✓ {0} ({1:N1} MB)" -f $exe, ((Get-Item "dist\$exe").Length / 1MB)) -ForegroundColor Green
}

if ($SkipInstaller) { return }

$Iscc = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) { throw "Inno Setup 6(ISCC.exe)을 찾을 수 없습니다: https://jrsoftware.org/isdl.php" }

& $Iscc "/DMyAppVersion=$Version" installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 실패 ($LASTEXITCODE)" }
Get-ChildItem dist\installer\*.exe | ForEach-Object {
    Write-Host ("  ✓ {0} ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB)) -ForegroundColor Green
}
