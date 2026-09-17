# Launch build_idf.bat in a hidden window and record launch timestamps.
# $PSScriptRoot is used so the script works from any checkout location.
$dir = $PSScriptRoot
$log = Join-Path $dir 'launcher.log'
"launched $(Get-Date -Format o)" | Out-File -FilePath $log -Encoding ascii
Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/c', "`"$dir\build_idf.bat`"" `
    -WorkingDirectory $dir `
    -WindowStyle Hidden
"start-process returned at $(Get-Date -Format o)" | Out-File -FilePath $log -Encoding ascii -Append
