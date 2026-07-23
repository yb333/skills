' ============================================================
' start_telemetry.vbs - 静默启动 Analyzer Agent Telemetry Server
'
' 双击运行：无窗口启动 node server.js，关掉终端/资源管理器后服务继续运行。
' 启动后自动检测健康状态，弹出提示，3秒后打开浏览器看板。
'
' 机制：WshShell.Run 第二参数 0=隐藏窗口，第三参数 False=进程独立存活。
'       VBS 脚本退出后，node 进程不属于任何终端会话，关窗口不停。
' ============================================================

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
Set WshShell = CreateObject("WScript.Shell")

' 进入 telemetry-server 目录
serverDir = scriptDir & "\telemetry-server"
WshShell.CurrentDirectory = serverDir

' 检测 Node.js
On Error Resume Next
nodePath = WshShell.RegRead("HKLM\SOFTWARE\nodejs\InstallPath\")
If Err.Number <> 0 Then
    nodePath = ""
End If
On Error GoTo 0

' 用 node（若注册表有路径则拼全，否则靠 PATH）
' 日志重定向到 telemetry.log（窗口隐藏后 stdout 不可见，需落盘便于排查）
logFile = serverDir & "\telemetry.log"
If nodePath <> "" Then
    nodeCmd = "cmd /c """"" & nodePath & "\node.exe"" server.js >> """ & logFile & """ 2>&1"""
Else
    nodeCmd = "cmd /c ""node server.js >> """ & logFile & """ 2>&1"""
End If

' 关键：0=隐藏窗口，False=进程独立（VBS退出后node继续跑）
WshShell.Run nodeCmd, 0, False

' 等待服务就绪（最多 15 秒）
ready = False
For i = 1 To 5
  WScript.Sleep 3000
  On Error Resume Next
  Set http = CreateObject("MSXML2.XMLHTTP")
  http.Open "GET", "http://localhost:3000/api/health", False
  http.Send
  If Err.Number = 0 And http.Status = 200 Then
    ready = True
    Exit For
  End If
  On Error GoTo 0
Next

If ready Then
  WshShell.Popup "Telemetry Server 已启动" & vbCrLf & _
                 "看板: http://localhost:3000/" & vbCrLf & _
                 "上报: http://本机IP:3000/api/usage", 5, "Analyzer Agent Telemetry", 64
  ' 5秒后打开浏览器
  WScript.Sleep 2000
  WshShell.Run "cmd /c start http://localhost:3000/", 0, False
Else
  WshShell.Popup "启动失败" & vbCrLf & _
                 "请检查 Node.js v22.5+ 是否安装" & vbCrLf & _
                 "日志: telemetry-server/telemetry.log", 0, "Analyzer Agent Telemetry", 16
End If
