$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$versions = @('3.15', '3.14', '3.13', '3.12', '3.11', '3.10')
$script:seen = @{}

function Get-UsablePython {
    param([string]$Command, [string[]]$Prefix = @())
    try {
        $result = & $Command @Prefix -c "import sys,struct,json; print(json.dumps({'exe':sys.executable,'version':list(sys.version_info[:2]),'bits':struct.calcsize('P')*8,'stable':sys.version_info.releaselevel=='final'}))" 2>$null
        if ($LASTEXITCODE -ne 0) { return }
        $info = ($result | Select-Object -Last 1) | ConvertFrom-Json
        if ($info.bits -ne 64 -or -not $info.stable -or $info.version[0] -ne 3 -or $info.version[1] -lt 10 -or $info.version[1] -gt 15) { return }
        if (-not $script:seen.ContainsKey($info.exe)) {
            $script:seen[$info.exe] = $true
            [PSCustomObject]@{ Path = $info.exe; Minor = [int]$info.version[1] }
        }
    } catch { return }
}

function Find-Python {
    $script:seen = @{}
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($version in $versions) { Get-UsablePython -Command 'py' -Prefix @("-$version") }
    }
    foreach ($command in @('python', 'python3')) {
        if (Get-Command $command -ErrorAction SilentlyContinue) { Get-UsablePython -Command $command }
    }
    foreach ($version in $versions) {
        $digits = $version.Replace('.', '')
        foreach ($path in @(
            "$env:LOCALAPPDATA\Programs\Python\Python$digits\python.exe",
            "$env:ProgramFiles\Python$digits\python.exe",
            "$env:LOCALAPPDATA\Python\pythoncore-$version-64\python.exe"
        )) {
            if (Test-Path -LiteralPath $path) { Get-UsablePython -Command $path }
        }
    }
}

function Start-WithPython {
    param($Candidate)
    $environment = Join-Path $PSScriptRoot ".runtime\py3$($Candidate.Minor)"
    $python = Join-Path $environment 'Scripts\python.exe'
    Write-Host "尝试 Python 3.$($Candidate.Minor) 64位：$($Candidate.Path)"
    if (-not (Test-Path -LiteralPath $python)) {
        & $Candidate.Path -m venv $environment
        if ($LASTEXITCODE -ne 0) { return $false }
    }
    & $python -m pip install --upgrade pip | Out-Host
    if ($LASTEXITCODE -ne 0) { return $false }
    $marker = Join-Path $environment 'requirements-installed.txt'
    $fingerprint = (Get-FileHash -LiteralPath 'requirements.txt' -Algorithm SHA256).Hash
    $ready = (Test-Path -LiteralPath $marker) -and ((Get-Content -LiteralPath $marker -Raw).Trim() -eq $fingerprint)
    if (-not $ready) {
        Write-Host '首次运行正在安装最新兼容依赖，需要联网，请耐心等待。'
        & $python -m pip install --upgrade --only-binary=:all: -r requirements.txt | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Host '该环境安装失败，可能缺少兼容安装包或网络连接失败，将尝试其他Python版本。'
            return $false
        }
        Set-Content -LiteralPath $marker -Value $fingerprint -Encoding ASCII
    }
    & $python -c "import streamlit,pandas,plotly; print('pandas:',pandas.__version__,'Streamlit:',streamlit.__version__)" | Out-Host
    if ($LASTEXITCODE -ne 0) { return $false }
    Write-Host '正在启动。请保持窗口打开；如果浏览器未打开，请访问 http://localhost:8501'
    & $python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless false | Out-Host
    if ($LASTEXITCODE -ne 0) { throw '应用启动失败，请查看上方错误。' }
    return $true
}

try {
    foreach ($file in @('app.py', 'database.py', 'requirements.txt')) {
        if (-not (Test-Path -LiteralPath $file)) { throw '文件不完整，请完整解压ZIP后再启动。' }
    }
    $candidates = @(Find-Python | Sort-Object Minor -Descending)
    foreach ($candidate in $candidates) {
        if (Start-WithPython -Candidate $candidate) { exit 0 }
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw '没有可用的兼容环境，且未找到winget。请安装64位Python 3.10至3.15中的稳定版并勾选Add Python to PATH，再运行脚本：https://www.python.org/downloads/windows/'
    }
    foreach ($version in $versions) {
        if ($candidates.Minor -contains [int]$version.Split('.')[1]) { continue }
        Write-Host "尝试通过winget安装Python $version 最新补丁版本。可能出现系统安装提示。"
        & winget install --id "Python.Python.$version" --exact --source winget --architecture x64 --accept-package-agreements --accept-source-agreements | Out-Host
        if ($LASTEXITCODE -ne 0) { continue }
        $newCandidates = @(Find-Python | Where-Object { $_.Minor -eq [int]$version.Split('.')[1] })
        foreach ($candidate in $newCandidates) {
            if (Start-WithPython -Candidate $candidate) { exit 0 }
        }
    }
    throw '未能安装可用环境。请检查网络；如Python刚安装完成，请关闭窗口后重新双击启动系统.bat。'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
