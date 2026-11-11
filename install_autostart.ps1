# Instala / desinstala el arranque automatico de ABOT en el Programador de tareas.
#
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Uninstall
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Status
#
# Crea la tarea "ABOT-Autostart":
#   - al iniciar sesion
#   - cada 5 minutos (red de seguridad si el watchdog muere)
# Lanza abot_watchdog.py con pythonw (sin ventana). El watchdog mantiene vivos
# Hummingbot (con la estrategia v2 ya iniciada) y supervisor_abot.py.

param(
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$TaskName = "ABOT-Autostart"
$Base = "C:\Users\rober\.config\opencode\hummingbot"
$PythonW = "C:\Users\rober\.conda\envs\hummingbot\pythonw.exe"
$Script = Join-Path $Base "abot_watchdog.py"

if ($Status) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "La tarea $TaskName NO existe."; exit 1 }
    $t | Format-List TaskName, State, Description
    $t.Triggers | Format-List
    (Get-ScheduledTaskInfo -TaskName $TaskName) | Format-List LastRunTime, LastTaskResult, NextRunTime
    exit 0
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Tarea $TaskName eliminada."
    } else {
        Write-Host "La tarea $TaskName no existia."
    }
    exit 0
}

if (-not (Test-Path $PythonW)) { throw "No encuentro pythonw.exe en $PythonW" }
if (-not (Test-Path $Script)) { throw "No encuentro $Script" }

$action = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$Script`"" -WorkingDirectory $Base

# 1) al iniciar sesion
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

# 2) cada 5 minutos (indefinido) como red de seguridad
$triggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

$desc = "ABOT: mantiene Hummingbot (conf_v6_global.yml) y supervisor_abot.py vivos. " +
        "No mata procesos; solo arranca lo que falta."

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($triggerLogon, $triggerRepeat) `
    -Settings $settings -Principal $principal -Description $desc -Force | Out-Null

Write-Host "Tarea '$TaskName' instalada."
Write-Host "  ejecuta : $PythonW $Script"
Write-Host "  dispara : al iniciar sesion + cada 5 min"
Write-Host ""
Write-Host "Comprobacion:  powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Status"
Write-Host "Parar todo :  python abot_watchdog.py --pause"
