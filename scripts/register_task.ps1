# Registra la tarea semanal (mismo esquema que "Cheil BI - Sprint Report"):
# viernes 09:00 hora local (Panama, UTC-5), sesion interactiva (Outlook y Codex usan tu perfil),
# se ejecuta al encender si el equipo estaba apagado, puede despertar el equipo, maximo 4 h.
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Unregister
param([switch]$Unregister, [string]$At = "09:00")
$name = "Cheil BI - CCA Promo Report"
if ($Unregister) { Unregister-ScheduledTask -TaskName $name -Confirm:$false; "Tarea eliminada: $name"; exit 0 }
$root = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction -Execute (Join-Path $root "scripts\weekly.cmd") -WorkingDirectory $root
# $At is Panama time (UTC-5, no daylight saving): convert it to this PC's local time.
$panama = [TimeZoneInfo]::FindSystemTimeZoneById("SA Pacific Standard Time")
$today = [DateTime]::Today
$friday = $today.AddDays(((5 - [int]$today.DayOfWeek) + 7) % 7)
$panamaStart = [DateTime]::ParseExact("$($friday.ToString('yyyy-MM-dd')) $At", "yyyy-MM-dd HH:mm", $null)
$localStart = [TimeZoneInfo]::ConvertTime($panamaStart, $panama, [TimeZoneInfo]::Local)
if ($localStart.DayOfWeek -ne [DayOfWeek]::Friday) { throw "En la zona horaria de este PC, $At Panama no cae en viernes; ajusta -At." }
if ([TimeZoneInfo]::Local.Id -ne $panama.Id) { "Aviso: el PC no esta en hora de Panama; $At Panama = $($localStart.ToString('HH:mm')) hora local." }
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At $localStart
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 4) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Weekly Competitor's Promotions Report: PDF + insights por correo (10:00 Panama)" -Force | Out-Null
$i = Get-ScheduledTask -TaskName $name | Get-ScheduledTaskInfo
"Tarea registrada: $name · proxima ejecucion $($i.NextRunTime)"
