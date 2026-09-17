Option Explicit
' AutoSelf local agent — hidden background (no console window)

Dim sh, fso, home, root, server, confDir, scriptDir, py, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

home = sh.ExpandEnvironmentStrings("%USERPROFILE%")
confDir = home & "\.autoself"
If Not fso.FolderExists(confDir) Then fso.CreateFolder(confDir)
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Function FindProject()
  Dim list, i, p
  list = Array( _
    "C:\social-auto-upload", _
    home & "\Applications\social-auto-upload", _
    home & "\social-auto-upload", _
    home & "\apps\social-auto-upload", _
    home & "\Desktop\social-auto-upload", _
    home & "\Documents\social-auto-upload" _
  )
  For i = 0 To UBound(list)
    p = list(i)
    If fso.FileExists(p & "\web_mvp\local_agent.py") Then
      FindProject = p
      Exit Function
    End If
  Next
  FindProject = ""
End Function

Function ChooseFolder()
  Dim shellApp, folder
  On Error Resume Next
  Set shellApp = CreateObject("Shell.Application")
  Set folder = shellApp.BrowseForFolder(0, "请选择本机的 social-auto-upload 项目文件夹（仅首次）", 0)
  If folder Is Nothing Then
    ChooseFolder = ""
  Else
    ChooseFolder = folder.Self.Path
  End If
End Function

If fso.FileExists(scriptDir & "\token") Then
  fso.CopyFile scriptDir & "\token", confDir & "\token", True
End If
If fso.FileExists(scriptDir & "\server") Then
  fso.CopyFile scriptDir & "\server", confDir & "\server", True
End If
If fso.FileExists(scriptDir & "\config.json") Then
  fso.CopyFile scriptDir & "\config.json", confDir & "\config.json", True
End If

If Not fso.FileExists(confDir & "\token") Then
  MsgBox "缺少云端连接凭证。请回到网页重新下载「本机助手」后再启动。", vbExclamation, "AutoSelf 本机助手"
  WScript.Quit 1
End If

root = FindProject()
If root = "" Then
  MsgBox "首次使用：请选择本机 social-auto-upload 项目文件夹。", vbInformation, "AutoSelf 本机助手"
  root = ChooseFolder()
End If
If root = "" Or Not fso.FileExists(root & "\web_mvp\local_agent.py") Then
  MsgBox "未找到项目（需要含 web_mvp\local_agent.py）。", vbExclamation, "AutoSelf 本机助手"
  WScript.Quit 1
End If

server = "http://autopost.com.cn"
If fso.FileExists(confDir & "\server") Then
  server = Trim(fso.OpenTextFile(confDir & "\server", 1).ReadAll())
End If
If LCase(Left(server, 8)) = "https://" And InStr(1, server, "autopost.com.cn", 1) > 0 Then
  server = "http://autopost.com.cn"
End If

If fso.FileExists(root & "\.venv\Scripts\python.exe") Then
  py = """" & root & "\.venv\Scripts\python.exe"""
Else
  py = "python"
End If

' 0 = hidden window, False = do not wait
cmd = "cmd /c cd /d """ & root & """ && " & py & " -m web_mvp.local_agent --server " & server
sh.Run cmd, 0, False

MsgBox "本机助手已在后台运行。" & vbCrLf & "看不到黑窗口是正常的，请回到网页点「我已启动，重新检测」。", vbInformation, "AutoSelf 本机助手"
