# weekly.ps1 - Friday by-law monitor run (Windows Task Scheduler entry point).
#
# Runs bm\weekly.py, then emails the HTML digest through the shared Gmail SMTP config
# (mb-parcelsearch\alert-email.local.txt, read by alert-lib.ps1 - the password never
# leaves that file). A failed run sends a failure alert (email + ntfy) instead.
# Log: logs\weekly-<stamp>.log ; ends with COMPLETE on success.
param([switch]$NoEmail, [switch]$NoFetch)

$ErrorActionPreference = 'Stop'
$root   = $PSScriptRoot
# Machine settings live in config.local.json (not in git); see config.example.json.
$cfgFile = Join-Path $root 'config.local.json'
if (-not (Test-Path $cfgFile)) { throw "config.local.json not found in $root. Copy config.example.json and set the folders." }
$conf   = Get-Content $cfgFile -Raw | ConvertFrom-Json
$mbps   = $conf.mb_parcelsearch_dir
if (-not $mbps) { throw 'mb_parcelsearch_dir is not set in config.local.json.' }
if (-not [IO.Path]::IsPathRooted($mbps)) { $mbps = [IO.Path]::GetFullPath((Join-Path $root $mbps)) }
$topic  = $conf.ntfy_topic
$stamp  = Get-Date -Format 'yyyyMMdd-HHmm'
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
$log    = Join-Path $logDir "weekly-$stamp.log"
. (Join-Path $mbps 'alert-lib.ps1')

function Send-HtmlDigest([string]$subject, [string]$htmlPath) {
  $cfg = Get-AlertConfig $mbps
  if (-not (Test-AlertEmailConfigured $cfg)) { Write-Warning 'Email not configured - digest not sent.'; return $false }
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $from = if ($cfg.from) { $cfg.from } else { $cfg.smtp_user }
  $msg = New-Object System.Net.Mail.MailMessage($from, $cfg.to)
  $msg.Subject = $subject
  $msg.Body = [IO.File]::ReadAllText($htmlPath, [Text.Encoding]::UTF8)
  $msg.IsBodyHtml = $true
  $msg.BodyEncoding = [Text.Encoding]::UTF8
  $port = if ($cfg.smtp_port) { [int]$cfg.smtp_port } else { 587 }
  $smtp = New-Object System.Net.Mail.SmtpClient($cfg.smtp_host, $port)
  $smtp.EnableSsl = $true
  $smtp.Timeout = 30000
  $smtp.Credentials = New-Object System.Net.NetworkCredential($cfg.smtp_user, $cfg.smtp_pass)
  try { $smtp.Send($msg) } finally { $msg.Dispose(); $smtp.Dispose() }
  return $true
}

$py = (Get-Command python -ErrorAction Stop).Source
$args_ = @('weekly.py'); if ($NoFetch) { $args_ += '--no-fetch' }
Push-Location (Join-Path $root 'bm')
try {
  $env:PYTHONIOENCODING = 'utf-8'
  $ErrorActionPreference = 'Continue'   # python's stderr must not become a terminating error
  # Not Tee-Object: in Windows PowerShell 5.1 it writes UTF-16, which breaks grep / log watchers.
  & $py @args_ 2>&1 | ForEach-Object { $line = "$_"; $line; Add-Content -Path $log -Value $line -Encoding UTF8 }
  $code = $LASTEXITCODE
} finally { Pop-Location; $ErrorActionPreference = 'Stop' }

$summaryPath = Join-Path $root 'data\weekly_latest.json'
$summary = if (Test-Path $summaryPath) { Get-Content $summaryPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
$fresh = $summary -and ((Get-Date) - [datetime]$summary.ran_at).TotalHours -lt 6

if (-not $fresh) {
  Send-FailureAlert $mbps $topic 'By-law monitor FAILED' "weekly.py exit $code; no fresh summary. Log: $log" | Out-Null
  Add-Content -Encoding UTF8 $log 'FAILED'
  exit 1
}
if (-not $NoEmail) {
  $subject = $summary.subject + $(if ($code -ne 0) { ' (with step errors)' } else { '' })
  if (Send-HtmlDigest $subject $summary.digest_path) { Add-Content -Encoding UTF8 $log "digest emailed: $subject" }
}
Add-Content -Encoding UTF8 $log 'COMPLETE'
exit 0
