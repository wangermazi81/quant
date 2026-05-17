Dim oShell
Set oShell = CreateObject("WScript.Shell")

oShell.Run "cmd /c for /f ""tokens=5"" %a in ('netstat -aon ^| findstr "":5001 """") do taskkill /PID %a /F >nul 2>&1", 0, True

WScript.Sleep 1500

oShell.Run """D:\软件用\python3.8.10\python.exe"" ""D:\同花顺量化交易\ths_auto_trader\main.py""", 1, False

Dim i, ready
ready = False
For i = 1 To 30
    WScript.Sleep 1000
    On Error Resume Next
    Dim oHTTP
    Set oHTTP = CreateObject("MSXML2.XMLHTTP")
    oHTTP.Open "GET", "http://127.0.0.1:5001/api/status", False
    oHTTP.Send
    If oHTTP.Status = 200 Then
        ready = True
        Exit For
    End If
    On Error GoTo 0
Next

If ready Then
    oShell.Run "http://127.0.0.1:5001", 1, False
    MsgBox "OK! http://127.0.0.1:5001", 64, "Launch OK"
Else
    MsgBox "Timeout.", 16, "Launch Failed"
End If
