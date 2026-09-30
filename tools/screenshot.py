"""Take a screenshot of the whole screen and print where it was saved.

  python tools\\screenshot.py            -> Pictures\\JARVIS\\screenshot-YYYYMMDD-HHMMSS.png
  python tools\\screenshot.py out.png    -> a specific path

JARVIS can then open the PNG with its Read tool to see what's on Dev's screen.
"""
import ctypes
import datetime
import os
import subprocess
import sys

ctypes.windll.user32.SetProcessDPIAware()  # capture at real resolution on scaled displays
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.expanduser(r"~\Pictures\JARVIS"), datetime.datetime.now().strftime("screenshot-%Y%m%d-%H%M%S.png"))
os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

ps = f"""
Add-Type -Name D -Namespace W -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
[W.D]::SetProcessDPIAware() | Out-Null
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
[System.Drawing.Graphics]::FromImage($bmp).CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$bmp.Save('{os.path.abspath(out)}', [System.Drawing.Imaging.ImageFormat]::Png)
"""
subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, creationflags=0x08000000)
print(os.path.abspath(out))
