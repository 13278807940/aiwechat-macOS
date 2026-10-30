[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')][string]$Repo,
    [string]$Ref = 'main',
    [ValidateSet('arm64', 'x86_64', 'both')][string]$Architecture = 'both',
    [switch]$Push,
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $projectRoot '下载的安装包' }
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw '请先安装 GitHub CLI：https://cli.github.com/，然后运行 gh auth login' }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw '请先安装 Git。' }

Push-Location -LiteralPath $projectRoot
try {
    & gh auth status
    if ($LASTEXITCODE -ne 0) { throw '请先运行 gh auth login 登录有仓库权限的 GitHub 账号。' }
    $gitRoot = (& git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0) { throw '请先把这个新文件夹作为独立 Git 仓库提交到 GitHub；详见 packaging/BUILD_MACOS.md。' }
    if ([IO.Path]::GetFullPath($gitRoot).TrimEnd('\', '/') -ne [IO.Path]::GetFullPath($projectRoot).TrimEnd('\', '/')) {
        throw '这个 Mac 项目必须作为仓库根目录；不能直接从外层 Windows 仓库运行。'
    }
    $pending = @(& git status --porcelain)
    if ($LASTEXITCODE -ne 0 -or $pending.Count -gt 0) { throw '请先提交代码更改。脚本不会自动提交文件，也不会上传 .env。' }
    if ($Push) {
        & git push "https://github.com/$Repo.git" "HEAD:refs/heads/$Ref"
        if ($LASTEXITCODE -ne 0) { throw '代码推送失败，未启动构建。' }
    }
    $localCommit = (& git rev-parse HEAD).Trim()
    $remoteCommit = (& gh api "repos/$Repo/commits/$Ref" --jq '.sha').Trim()
    if ($LASTEXITCODE -ne 0 -or $localCommit -ne $remoteCommit) {
        throw '远程分支与本地提交不一致。先推送代码，或使用 -Push；避免打到旧代码。'
    }
    $requestId = [Guid]::NewGuid().ToString('N')
    & gh workflow run build-macos.yml --repo $Repo --ref $Ref -f "architecture=$Architecture" -f "request_id=$requestId"
    if ($LASTEXITCODE -ne 0) { throw '构建启动失败。确认 workflow 已在 GitHub 默认分支、Actions 已启用且有运行额度。' }

    $runId = $null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $runJson = & gh run list --repo $Repo --workflow build-macos.yml --event workflow_dispatch --limit 30 --json databaseId,displayTitle,headSha
        if ($LASTEXITCODE -ne 0) { throw '无法查询构建，请打开 GitHub Actions 查看。' }
        $matchingRun = @($runJson | ConvertFrom-Json) | Where-Object { $_.displayTitle -like "*$requestId*" -and $_.headSha -eq $localCommit } | Select-Object -First 1
        if ($matchingRun) { $runId = $matchingRun.databaseId; break }
        Start-Sleep -Seconds 3
    }
    if (-not $runId) { throw "已发起构建但尚未找到运行记录。请在仓库 Actions 查找请求 $requestId。不要重复提交。" }
    Write-Host "macOS 构建已启动：https://github.com/$Repo/actions/runs/$runId"
    & gh run watch $runId --repo $Repo --exit-status
    if ($LASTEXITCODE -ne 0) { throw 'macOS 构建失败，请查看上面的运行日志。' }
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    & gh run download $runId --repo $Repo --dir $OutputDirectory
    if ($LASTEXITCODE -ne 0) { throw '构建已完成，下载失败；可从 Actions 页面手动下载。' }
    Write-Host "Mac 安装包已下载到：$OutputDirectory"
}
finally { Pop-Location }
