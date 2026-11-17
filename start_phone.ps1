# Start the virtual Android phone (AVD "trialbot") and wait until it is ready.
$env:ANDROID_HOME = "$HOME\android\sdk"
$env:ANDROID_SDK_ROOT = "$HOME\android\sdk"
$adb = "$HOME\android\sdk\platform-tools\adb.exe"
$dev = "emulator-5554"

if (-not (Get-Process qemu-system* -ErrorAction SilentlyContinue)) {
    Start-Process "$HOME\android\sdk\emulator\emulator.exe" -ArgumentList @(
        "-avd", "trialbot", "-gpu", "auto", "-no-boot-anim", "-no-snapshot",
        "-no-audio", "-dns-server", "8.8.8.8,1.1.1.1")
}

& $adb -s $dev wait-for-device
for ($i = 0; $i -lt 60; $i++) {
    if ((& $adb -s $dev shell getprop sys.boot_completed 2>$null) -eq "1") { break }
    Start-Sleep 5
}
foreach ($k in "window_animation_scale", "transition_animation_scale", "animator_duration_scale") {
    & $adb -s $dev shell settings put global $k 0
}

# keep the window fully on screen
Add-Type @"
using System; using System.Runtime.InteropServices;
public class PhoneWin { [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h,int x,int y,int w,int ht,bool r); }
"@
Start-Sleep 2
$h = (Get-Process qemu-system* | Where-Object MainWindowHandle -ne 0 | Select-Object -First 1).MainWindowHandle
if ($h) { [PhoneWin]::MoveWindow($h, 580, 10, 380, 830, $true) | Out-Null }
Write-Output "phone ready"
