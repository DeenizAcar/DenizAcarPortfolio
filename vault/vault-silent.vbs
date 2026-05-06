' VAULT — sessiz başlatıcı (terminal penceresi açmaz)
' Çift tıkla, sunucu arka planda başlar, tarayıcıda açılır.
' Kapatmak için: Görev Yöneticisi'nden python.exe görevini sonlandır.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run "py vault.py", 0, False
