' Investment Micro-System — hidden launcher
' Point your desktop shortcut at this file.
' To stop:   Stop.bat        (same folder)
' To update: Update Now.bat  (same folder)
'
' Modelled on the vault's "Coverage Dashboard.vbs", with three differences:
'   - python packages/api/serve.py, not node serve.js
'   - PORT 8770. 8765 is the vault's node dashboard and has been up since
'     13 Aug. A clash there does NOT error: the other server answers and every
'     request 404s as if the routes were broken.
'   - refreshes the scores first, so double-clicking the shortcut in the
'     morning gives today's numbers rather than whenever it was last run.

Dim PORT, URL, TIMEOUT, REFRESH_ON_LAUNCH
PORT    = "8770"
URL     = "http://127.0.0.1:" & PORT
TIMEOUT = 25
' Set to False if you would rather the shortcut only opened the page. The
' refresh is idempotent — run_scores.py rewrites today's rows rather than
' appending duplicates — so launching twice in a day is harmless.
REFRESH_ON_LAUNCH = True

Dim shell, fso, repo, servePy, refreshPy, tmpFile, f, outText, i, wasLive

Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

' The repo root is one folder UP from this launch\ directory.
repo      = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
servePy   = fso.BuildPath(repo, "packages\api\serve.py")
refreshPy = fso.BuildPath(repo, "packages\refresh.py")

tmpFile = shell.ExpandEnvironmentStrings("%TEMP%") & "\_ims_port_chk.txt"

Function IsPortListening()
    shell.Run "cmd /c netstat -ano | findstr /C:""127.0.0.1:" & PORT & """ | findstr /C:""LISTENING"" > """ & tmpFile & """", 0, True
    If fso.FileExists(tmpFile) Then
        Set f = fso.GetFile(tmpFile)
        If f.Size > 0 Then
            outText = fso.OpenTextFile(tmpFile, 1).ReadAll
            fso.DeleteFile tmpFile, True
            IsPortListening = (Len(Trim(outText)) > 0)
            Exit Function
        End If
        fso.DeleteFile tmpFile, True
    End If
    IsPortListening = False
End Function

wasLive = IsPortListening()

' --- Refresh the scores (blocking, hidden) -------------------------------
' Runs even when the server is already up: the server reads SQLite per request,
' so new rows are visible on the next page load with no restart. Failure is
' deliberately NOT fatal here — a stale dashboard you can see beats no
' dashboard at all, and the page's own refresh light reports the staleness.
If REFRESH_ON_LAUNCH Then
    shell.Run "cmd /c python """ & refreshPy & """", 0, True
End If

If wasLive Then
    shell.Run """" & URL & """", 1, False
    WScript.Quit 0
End If

' --- Start the server completely hidden ----------------------------------
shell.CurrentDirectory = repo
shell.Run "cmd /c python """ & servePy & """", 0, False

' --- Poll until the port binds -------------------------------------------
For i = 1 To (TIMEOUT * 2)
    WScript.Sleep 500
    If IsPortListening() Then Exit For
Next

If Not IsPortListening() Then
    MsgBox "The server did not come up on port " & PORT & " within " & _
           TIMEOUT & " seconds." & vbCrLf & vbCrLf & _
           "Run this to see the error:" & vbCrLf & _
           "  python """ & servePy & """", 48, "Investment Micro-System"
    WScript.Quit 1
End If

shell.Run """" & URL & """", 1, False
