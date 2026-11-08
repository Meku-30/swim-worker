# GUI の待機中の重さを測る (M1)。exe を起動して 60 秒待ち、10 秒おきに 6 回、
# プロセスとその子 (PyInstaller の onefile は親と子の 2 つ) の合計を取り、中央値を出す。
# 使い方: pwsh tools/measure_gui_windows.ps1 -Name <名前> -Exe <exe のパス> -Out <結果の csv に追記>
param([string]$Name, [string]$Exe, [string]$Out)
$ErrorActionPreference = 'Stop'

function Get-Tree([int]$RootId) {
    $all = Get-CimInstance Win32_Process
    $ids = @($RootId)
    $added = $true
    while ($added) {
        $added = $false
        foreach ($p in $all) {
            if ($ids -contains [int]$p.ParentProcessId -and -not ($ids -contains [int]$p.ProcessId)) {
                $ids += [int]$p.ProcessId; $added = $true
            }
        }
    }
    return $ids
}

function Median($xs) { $s = $xs | Sort-Object; return $s[[int][math]::Floor($s.Count / 2)] }

$proc = Start-Process -FilePath $Exe -PassThru
Start-Sleep -Seconds 60
$ws = @(); $priv = @(); $cpu = @()
for ($i = 0; $i -lt 6; $i++) {
    $tree = Get-Tree $proc.Id
    $ps = Get-Process -Id $tree -ErrorAction SilentlyContinue
    $ws += ($ps | Measure-Object WorkingSet64 -Sum).Sum
    $priv += ($ps | Measure-Object PrivateMemorySize64 -Sum).Sum
    $cpu += ($ps | Measure-Object TotalProcessorTime -Property TotalProcessorTime -Sum -ErrorAction SilentlyContinue).Sum
    if ($i -lt 5) { Start-Sleep -Seconds 10 }
}
$tree = Get-Tree $proc.Id
$cpuTotal = (Get-Process -Id $tree -ErrorAction SilentlyContinue | ForEach-Object { $_.TotalProcessorTime.TotalSeconds } | Measure-Object -Sum).Sum
foreach ($id in $tree) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
$size = (Get-Item $Exe).Length
$line = "{0},{1:N1},{2:N1},{3:N2},{4:N1},{5}" -f $Name, ((Median $ws) / 1MB), ((Median $priv) / 1MB), $cpuTotal, ($size / 1MB), $tree.Count
if (-not (Test-Path $Out)) { "name,working_set_mb,private_mb,cpu_secs_110s,exe_mb,processes" | Out-File $Out -Encoding utf8 }
$line | Out-File $Out -Append -Encoding utf8
Write-Output $line
