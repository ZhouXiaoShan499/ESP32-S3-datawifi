$ErrorActionPreference = "Continue"

Write-Host "== Windows microphone privacy =="
foreach ($path in @(
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone",
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
)) {
    if (Test-Path $path) {
        $value = (Get-ItemProperty -LiteralPath $path).Value
        Write-Host "$path -> $value"
    } else {
        Write-Warning "$path is missing"
    }
}

Write-Host ""
Write-Host "== Windows capture endpoints =="
$root = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
$rows = @()
if (Test-Path $root) {
    foreach ($device in Get-ChildItem $root) {
        $propsPath = Join-Path $device.PSPath "Properties"
        $props = Get-ItemProperty -LiteralPath $propsPath -ErrorAction SilentlyContinue
        $state = [int](Get-ItemProperty -LiteralPath $device.PSPath -ErrorAction SilentlyContinue).DeviceState
        $states = @()
        if ($state -band 1) { $states += "ACTIVE" }
        if ($state -band 2) { $states += "DISABLED" }
        if ($state -band 4) { $states += "NOT_PRESENT" }
        if ($state -band 8) { $states += "UNPLUGGED" }
        $rows += [PSCustomObject]@{
            Name = $props.'{a45c254e-df1c-4efd-8020-67d146a850e0},2'
            StateCode = $state
            State = ($states -join "|")
        }
    }
}
$rows | Sort-Object StateCode,Name | Format-Table -AutoSize
$active = @($rows | Where-Object { $_.StateCode -band 1 })
if ($active.Count -eq 0) {
    Write-Warning "No ACTIVE capture endpoint. Connect/enable a microphone in Windows Sound settings."
} else {
    Write-Host "Active input endpoint count: $($active.Count)"
}

Write-Host ""
Write-Host "== Edge microphone policy =="
foreach ($path in @(
    "HKLM:\SOFTWARE\Policies\Microsoft\Edge",
    "HKCU:\SOFTWARE\Policies\Microsoft\Edge"
)) {
    if (Test-Path $path) {
        $p = Get-ItemProperty -LiteralPath $path
        Write-Host "$path -> AudioCaptureAllowed=$($p.AudioCaptureAllowed)"
    } else {
        Write-Host "$path -> no policy override"
    }
}

Write-Host ""
Write-Host "If no endpoint is ACTIVE, open:"
Write-Host "  Settings > System > Sound > Input"
Write-Host "Then select a connected input device and test its level."
if ($active.Count -eq 0) { exit 1 }
exit 0
