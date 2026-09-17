$dir = 'd:\v20699\data_capture_sim (2)\data_capture_sim\data_capture_sim'
$log = Join-Path $dir 'launcher.log'
"launched $(Get-Date -Format o)" | Out-File -FilePath $log -Encoding ascii
Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/c', "`"$dir\build_idf.bat`"" `
    -WorkingDirectory $dir `
    -WindowStyle Hidden
"start-process returned at $(Get-Date -Format o)" | Out-File -FilePath $log -Encoding ascii -Append
