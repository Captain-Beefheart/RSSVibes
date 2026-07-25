' RSSVibes.vbs — double-click to launch RSSVibes as a desktop app.
' Runs rssvibes.ps1 hidden, so no console window appears — just the app window.
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & dir & "\rssvibes.ps1""", 0, False
