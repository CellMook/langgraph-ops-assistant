<#
.SYNOPSIS
  一键把本仓库发布到 GitHub：git init → 自检 → 提交 → 创建/关联远程 → push → 验证。

.DESCRIPTION
  两种模式：
    A) 有 GitHub CLI（gh）且已登录：自动创建远程仓库并推送。
    B) 没有 gh（或 gh 不在 PATH）：先在网页上建好空仓库，再用 -RemoteUrl 传入地址，
       脚本只做 init/自检/提交/关联/推送，不需要 gh。

  安全设计：
    - 提交前检查暂存区，若包含 .env / *.db / Academy课程 / 内部笔记等，直接中止；
    - -DryRun 只打印计划，不执行任何写操作；
    - 不修改 .gitignore 与任何源码。

  注意：本脚本在生成环境中**无法被运行验证**（该环境不能启动进程），
  因此首次使用时请先跑 -DryRun，确认打印的计划符合预期再正式执行。

  运行方式（在仓库根目录）：
      pwsh -ExecutionPolicy Bypass -File .\publish.ps1 -DryRun
      pwsh -ExecutionPolicy Bypass -File .\publish.ps1 -RepoName langgraph-ops-assistant
      pwsh -ExecutionPolicy Bypass -File .\publish.ps1 -RemoteUrl https://github.com/you/langgraph-ops-assistant.git
    没有 pwsh（PowerShell 7）时，把命令里的 pwsh 换成 powershell。

.EXAMPLE
  .\publish.ps1 -DryRun

.EXAMPLE
  .\publish.ps1 -RemoteUrl https://github.com/yourname/langgraph-ops-assistant.git -DryRun

.EXAMPLE
  .\publish.ps1 -RemoteUrl https://github.com/yourname/langgraph-ops-assistant.git
#>
[CmdletBinding()]
param(
    [string]$RepoName = "langgraph-ops-assistant",
    [switch]$Private,
    [string]$RemoteUrl = "",
    [string]$CommitMessage = "feat: 经营数据分析与知识库智能助手（LangGraph 工作流 + 22 单元教程）",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
Push-Location $repoRoot

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Stop-WithError {
    param([string]$Message)
    Write-Host ""
    Write-Host "错误：$Message" -ForegroundColor Red
    if ((Get-Location -Stack).Count -gt 0) {
        Pop-Location
    }
    exit 1
}

function Invoke-Plan {
    param([string]$Command)
    if ($DryRun) {
        Write-Host "[dry-run] $Command" -ForegroundColor DarkYellow
    }
}

try {
    Write-Host "仓库目录：$repoRoot"
    if ($RemoteUrl) {
        Write-Host "远程地址：$RemoteUrl（模式 B：不依赖 gh）"
    } else {
        Write-Host "远程地址：由 gh 自动创建（模式 A）"
    }

    # ---------------------------------------------------------------- 1. 工具链
    Write-Step "检查工具链"

    $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    if (-not $gitCmd) {
        Stop-WithError "未找到 git。请安装 Git for Windows 后重开终端：winget install Git.Git"
    }
    Write-Host ("git : {0}" -f $gitCmd.Source)

    $login = ""
    $userId = ""
    $ghCmd = Get-Command gh -ErrorAction SilentlyContinue

    if ($RemoteUrl) {
        # 模式 B：从 URL 里取 owner，仅用于打印与兜底邮箱
        $match = [regex]::Match($RemoteUrl, 'github\.com[:/](?<owner>[^/]+)/')
        if ($match.Success) {
            $login = $match.Groups['owner'].Value
        }
    } else {
        if (-not $ghCmd) {
            Write-Host ""
            Write-Host "未找到 gh。可以任选其一：" -ForegroundColor Yellow
            Write-Host "  1) 安装后重开终端：winget install --id GitHub.cli -e --source winget"
            Write-Host "  2) 不用 gh：先到 https://github.com/new 建空仓库，再运行"
            Write-Host "     .\publish.ps1 -RemoteUrl https://github.com/<你的用户名>/<仓库名>.git"
            Stop-WithError "缺少 gh，且未提供 -RemoteUrl。"
        }
        Write-Host ("gh  : {0}" -f $ghCmd.Source)

        Write-Step "检查 gh 登录状态"
        $authOutput = (& gh auth status 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) {
            Stop-WithError "gh 尚未登录。请先执行：gh auth login`n$authOutput"
        }
        $login = (& gh api user --jq .login 2>$null | Out-String).Trim()
        if (-not $login) {
            Stop-WithError "无法通过 gh api user 获取用户名，请检查网络或代理后重试。"
        }
        $userId = (& gh api user --jq .id 2>$null | Out-String).Trim()
        Write-Host "已登录账号：$login"
    }

    # ------------------------------------------------------------ 2. git 身份
    Write-Step "检查 git 提交身份"

    $gitName = (& git config user.name 2>$null | Out-String).Trim()
    $gitEmail = (& git config user.email 2>$null | Out-String).Trim()

    if (-not $gitName) {
        if ($login) {
            if ($DryRun) {
                Invoke-Plan "git config user.name $login"
            } else {
                & git config user.name $login
                Write-Host "已设置 user.name = $login（本仓库级）"
            }
        } else {
            Stop-WithError "git 未配置 user.name，且无法推断 GitHub 用户名。请先执行：git config --global user.name `"你的名字`""
        }
    }
    if (-not $gitEmail) {
        if ($userId) {
            $fallbackEmail = "$userId+$login@users.noreply.github.com"
        } elseif ($login) {
            $fallbackEmail = "$login@users.noreply.github.com"
        } else {
            $fallbackEmail = ""
        }
        if (-not $fallbackEmail) {
            Stop-WithError "git 未配置 user.email。请先执行：git config --global user.email `"你的邮箱`""
        }
        if ($DryRun) {
            Invoke-Plan "git config user.email $fallbackEmail"
        } else {
            & git config user.email $fallbackEmail
            Write-Host "已设置 user.email = $fallbackEmail（GitHub 匿名邮箱，不暴露真实邮箱）"
        }
    }

    # -------------------------------------------------------------- 3. 初始化
    Write-Step "初始化 / 确认 git 仓库"

    if (Test-Path (Join-Path $repoRoot ".git")) {
        Write-Host "已存在 .git，跳过 init"
    } else {
        if ($DryRun) {
            Invoke-Plan "git init -b main"
        } else {
            & git init -b main | Out-Null
            if ($LASTEXITCODE -ne 0) {
                & git init | Out-Null
                & git branch -M main
            }
            Write-Host "已初始化仓库（分支 main）"
        }
    }

    $currentBranch = (& git symbolic-ref --short HEAD 2>$null | Out-String).Trim()
    if ($currentBranch -and $currentBranch -ne "main") {
        Write-Host "当前分支为 $currentBranch，将重命名为 main"
        if (-not $DryRun) {
            & git branch -M main
        }
    }

    # ---------------------------------------------------------- 4. 暂存与自检
    Write-Step "暂存文件并执行安全检查"

    if ($DryRun) {
        Invoke-Plan "git add -A"
    } else {
        & git add -A
    }

    $staged = @(& git ls-files)
    Write-Host ("暂存文件数：{0}" -f $staged.Count)

    $badPattern = '(?i)(^|/)(Academy课程|briefs)/' +
        '|(^|/)(MISSION|NOTES|RESOURCES)\.md$' +
        '|(^|/)\.env$' +
        '|\.db$|\.db-wal$|\.db-shm$' +
        '|knowledge_index\.json$' +
        '|(^|/)data/exports/' +
        '|(^|/)eval/results/' +
        '|__pycache__|\.venv/|\.pytest_cache/'

    $badFiles = @($staged | Where-Object { $_ -match $badPattern })
    if ($badFiles.Count -gt 0) {
        Write-Host "以下文件不应进入仓库：" -ForegroundColor Red
        $badFiles | ForEach-Object { Write-Host ("  - {0}" -f $_) -ForegroundColor Red }
        Stop-WithError "请检查根目录 .gitignore；若文件已被 git 追踪，先执行：git rm --cached <路径>"
    }
    Write-Host "安全检查通过：没有密钥、数据库、第三方课程目录或个人笔记" -ForegroundColor Green

    # -------------------------------------------------------------- 5. 提交
    Write-Step "提交"

    $porcelain = @(& git status --porcelain)
    if ($porcelain.Count -eq 0) {
        Write-Host "工作区没有新变更，跳过 commit"
    } else {
        if ($DryRun) {
            Invoke-Plan "git commit -m `"$CommitMessage`""
        } else {
            & git commit -m $CommitMessage | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Stop-WithError "git commit 失败，请查看上方输出（常见原因：身份未配置、文件被占用）。"
            }
            Write-Host "提交完成"
        }
    }

    # -------------------------------------------------------- 6. 关联并推送
    Write-Step "关联远程仓库并推送"

    $hasOrigin = $false
    & git remote get-url origin 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $hasOrigin = $true
    }

    if ($RemoteUrl) {
        if ($hasOrigin) {
            Write-Host "已存在 origin，更新为 $RemoteUrl"
            if ($DryRun) {
                Invoke-Plan "git remote set-url origin $RemoteUrl"
            } else {
                & git remote set-url origin $RemoteUrl
            }
        } else {
            if ($DryRun) {
                Invoke-Plan "git remote add origin $RemoteUrl"
            } else {
                & git remote add origin $RemoteUrl
            }
        }
        if ($DryRun) {
            Invoke-Plan "git push -u origin main"
        } else {
            & git push -u origin main
            if ($LASTEXITCODE -ne 0) {
                Stop-WithError "git push 失败。常见原因：① 远程仓库不是空的（先 git pull --rebase origin main）② 认证失败（用 PAT 或先登录 Git Credential Manager）。"
            }
        }
    } elseif ($hasOrigin) {
        Write-Host "已存在 origin，直接推送"
        if ($DryRun) {
            Invoke-Plan "git push -u origin main"
        } else {
            & git push -u origin main
            if ($LASTEXITCODE -ne 0) {
                Stop-WithError "git push 失败。若远程已有提交，先执行 git pull --rebase origin main 再重试。"
            }
        }
    } else {
        if ($Private) {
            $visibilityFlag = "--private"
        } else {
            $visibilityFlag = "--public"
        }
        if ($DryRun) {
            Invoke-Plan "gh repo create $RepoName $visibilityFlag --source . --remote origin --push"
        } else {
            & gh repo create $RepoName $visibilityFlag --source . --remote origin --push
            if ($LASTEXITCODE -ne 0) {
                Write-Host "gh repo create 失败。若同名仓库已存在，可改用：" -ForegroundColor Yellow
                Write-Host ("  git remote add origin https://github.com/{0}/{1}.git" -f $login, $RepoName)
                Write-Host "  git push -u origin main"
                Stop-WithError "远程创建或推送未完成。"
            }
        }
    }

    # -------------------------------------------------------------- 7. 验证
    Write-Step "验证"

    if ($DryRun) {
        Write-Host "[dry-run] 未执行任何写操作；去掉 -DryRun 重新运行即可真正发布。"
    } else {
        & git status --short
        Write-Host ""
        if ($login -and $RemoteUrl) {
            Write-Host ("完成。请在浏览器打开：https://github.com/{0}/{1}" -f $login, $RepoName) -ForegroundColor Green
        } elseif ($login) {
            Write-Host ("完成。仓库地址：https://github.com/{0}/{1}" -f $login, $RepoName) -ForegroundColor Green
        } else {
            Write-Host "完成。请到 GitHub 上确认仓库内容。" -ForegroundColor Green
        }
        Write-Host "建议下一步："
        Write-Host "  1) 生成并提交锁文件：cd ops-assistant; uv lock; cd ..; git add ops-assistant/uv.lock; git commit -m 'chore: 提交 uv.lock'; git push"
        Write-Host "  2) 按 docs/verification.md 跑一遍，把真实结果补进仓库"
    }
}
finally {
    if ((Get-Location -Stack).Count -gt 0) {
        Pop-Location
    }
}
