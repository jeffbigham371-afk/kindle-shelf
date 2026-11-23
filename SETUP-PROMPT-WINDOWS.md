You are setting up a self-hosted web app called "Kindle Shelf" on this Windows Server 2016 machine (build 14393). Run everything in PowerShell as Administrator. Work step by step. After each step, verify it worked before moving on. If a step fails, stop and show me the exact error. Don't improvise around it.

Rules:
- Windows Server 2016 has no winget. Download installers directly.
- Before any download, run: [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
- Don't use Docker. The app runs directly on Windows.
- Don't stop, change, or uninstall any existing services or software. Only open the one firewall port listed below.
- Ask me before rebooting.

STEP 1 - Make modern TLS permanent for .NET:
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\.NETFramework\v4.0.30319' -Name SchUseStrongCrypto -Value 1 -Type DWord
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Wow6432Node\Microsoft\.NETFramework\v4.0.30319' -Name SchUseStrongCrypto -Value 1 -Type DWord

STEP 2 - Install Python 3.12 (64-bit) for all users:
Download https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe to $env:TEMP and run it with:
/quiet InstallAllUsers=1 PrependPath=1 Include_test=0
Verify with: & "C:\Program Files\Python312\python.exe" --version

STEP 3 - Install Calibre (it does the ebook conversion):
Download https://calibre-ebook.com/dist/win64 to $env:TEMP\calibre.msi and install with: msiexec /i "$env:TEMP\calibre.msi" /qn /norestart (wait for it to finish).
Verify with: & "C:\Program Files\Calibre2\ebook-convert.exe" --version
If ebook-convert.exe fails to start or crashes on this OS, uninstall that Calibre and install version 5.44 instead:
https://download.calibre-ebook.com/5.44.0/calibre-64bit-5.44.0.msi
Then verify again.

STEP 4 - Get the app code:
git clone REPO_URL_HERE C:\kindle-shelf

STEP 5 - Install the Python dependencies in a virtual environment:
cd C:\kindle-shelf
& "C:\Program Files\Python312\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

STEP 6 - Create C:\kindle-shelf\start.ps1 with exactly this content:
$env:Path = "C:\Program Files\Calibre2;" + $env:Path
$env:DATA_DIR = "C:\kindle-shelf\data"
$env:APP_USER = "admin"
$env:APP_PASSWORD = "CHANGE_ME"
$env:KINDLE_PROFILE = "kindle_pw3"
Set-Location C:\kindle-shelf
& C:\kindle-shelf\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080 *>> C:\kindle-shelf\server.log
Then ask me what password I want and replace CHANGE_ME with it.

STEP 7 - Run the app automatically at boot:
Register a scheduled task named "Kindle Shelf" that runs at system startup as SYSTEM with highest privileges. The action is:
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\kindle-shelf\start.ps1
Set it to restart on failure (3 times, 1 minute apart) and to have no time limit (ExecutionTimeLimit 0). Then start the task now.

STEP 8 - Open the firewall for the app only:
New-NetFirewallRule -DisplayName "Kindle Shelf 8080" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow

STEP 9 - Verify:
Wait 10 seconds, then run (using the password from step 6):
$cred = New-Object PSCredential("admin", (ConvertTo-SecureString "THE_PASSWORD" -AsPlainText -Force))
Invoke-RestMethod http://localhost:8080/api/config -Credential $cred
It must return "calibre": true. If the request fails, show me the last 40 lines of C:\kindle-shelf\server.log.

When everything passes, tell me the server's LAN IP address (from Get-NetIPAddress -AddressFamily IPv4) so I can open http://THAT_IP:8080 in a browser.
