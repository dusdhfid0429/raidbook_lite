Set objFSO   = CreateObject("Scripting.FileSystemObject")
Set objShell = CreateObject("WScript.Shell")

' 자식 봇 프로세스의 이모지 출력이 cp949 파이프에서 깨지지 않도록 UTF-8 모드
objShell.Environment("PROCESS")("PYTHONUTF8") = "1"

strDir    = objFSO.GetParentFolderName(WScript.ScriptFullName)
strScript = strDir & "\RaidBook_Watchdog_GUI.py"

' pythonw.exe: 콘솔 창 없이 실행 (프로젝트 venv 우선)
strPythonW = strDir & "\.venv\Scripts\pythonw.exe"

If objFSO.FileExists(strPythonW) Then
    objShell.Run """" & strPythonW & """ """ & strScript & """", 0, False
Else
    ' 경로가 없으면 PATH에서 pythonw 시도
    objShell.Run "pythonw """ & strScript & """", 0, False
End If
