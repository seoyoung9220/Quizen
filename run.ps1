# Quizen 실행
#   .\run.ps1          이 PC에서만 열기 (http://127.0.0.1:8501)
#   .\run.ps1 -Remote  내 Tailscale 기기(맥북·휴대폰 등)에서도 열기 (http://<이 PC의 Tailscale 주소>:8501)
#
# -Remote는 Tailscale 주소에만 연결을 연다. 공유기 안의 다른 기기나 인터넷에서는 열리지 않는다
# (0.0.0.0 처럼 모든 네트워크에 여는 설정은 쓰지 않는다).
param([switch]$Remote)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = Join-Path $env:USERPROFILE ".venvs\quizen\Scripts\python.exe"
$addr = "127.0.0.1"

if ($Remote) {
    $ts = Get-Command tailscale -ErrorAction SilentlyContinue
    if (-not $ts) { throw "Tailscale이 설치되어 있지 않습니다." }
    $addr = (& tailscale ip -4 | Select-Object -First 1).Trim()
    if ($addr -notmatch '^100\.') { throw "Tailscale 주소를 얻지 못했습니다. Tailscale에 로그인되어 있는지 확인하세요." }
    Write-Host "내 Tailscale 기기에서 접속: http://${addr}:8501  (이 PC에서도 같은 주소로 열기)"
}

& $py -m streamlit run app.py --server.address $addr
