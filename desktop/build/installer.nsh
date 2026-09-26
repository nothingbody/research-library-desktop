; Keep installation, personal data and the browser launcher separate.
!ifndef RL_INSTALLER_INCLUDED
!define RL_INSTALLER_INCLUDED
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "install-files.nsh"
!define /ifndef RL_PROTOCOL "researchlibrary"

!ifdef BUILD_UNINSTALLER
  !define RL_FUNCTION_PREFIX "un."
!else
  !define RL_FUNCTION_PREFIX ""
!endif

Var RL_Path
Var RL_CanonicalPath
Var RL_Result
Var RL_Error
Var RL_LegacyAllowed
Var RL_AllowUnknown
Var RL_InspectDirectory
Var RL_InspectRelative
!ifndef BUILD_UNINSTALLER
  Var RL_OriginalInstallDirectory
!endif

!macro RL_Stop MESSAGE
  DetailPrint "${MESSAGE}"
  MessageBox MB_OK|MB_ICONSTOP "${MESSAGE}" /SD IDOK
  SetErrorLevel 3
  Quit
!macroend

; Reject root directories, relative/device paths and every reparse-point
; ancestor. Check non-existing targets through their existing parents too.
; electron-builder includes this file before common.nsh defines executable
; names. Emit functions through customHeader after those definitions exist.
!macro customHeader
Function ${RL_FUNCTION_PREFIX}RL_CheckSafePath
  Push $0
  Push $1
  Push $2
  Push $3
  StrCpy $RL_Result "0"
  StrCpy $RL_Error "安装或卸载目录不安全，请选择独立的本地程序目录。"
  StrCmp $RL_Path "" rl_safe_done
  StrCpy $0 $RL_Path 4
  StrCmp $0 "\\?\" rl_safe_done
  StrCmp $0 "\\.\" rl_safe_done
  StrCpy $0 $RL_Path 2
  StrCmp $0 "\\" rl_safe_done
  StrCpy $0 $RL_Path 1 1
  StrCmp $0 ":" 0 rl_safe_done
  StrCpy $0 $RL_Path 1 2
  StrCmp $0 "\" 0 rl_safe_done
  ClearErrors
  GetFullPathName $RL_CanonicalPath "$RL_Path"
  IfErrors rl_safe_done
  StrCmp $RL_CanonicalPath "" rl_safe_done
  ${GetRoot} "$RL_CanonicalPath" $0
  StrCmp $0 "" rl_safe_done
  StrCmp $RL_CanonicalPath "$0" rl_safe_done
  StrCmp $RL_CanonicalPath "$0\" rl_safe_done
  ; Windows strips some trailing periods/spaces; avoid checking one path
  ; and then extracting or deleting another spelling of it.
  StrLen $1 $RL_Path
  StrCpy $2 0
  rl_safe_char_loop:
  IntCmp $2 $1 rl_safe_last_component
  StrCpy $3 $RL_Path 1 $2
  StrCmp $3 "*" rl_safe_done
  StrCmp $3 "?" rl_safe_done
  StrCmp $3 '$\"' rl_safe_done
  StrCmp $3 "<" rl_safe_done
  StrCmp $3 ">" rl_safe_done
  StrCmp $3 "|" rl_safe_done
  StrCmp $3 "\" 0 rl_safe_check_colon
  IntOp $3 $2 - 1
  StrCpy $3 $RL_Path 1 $3
  StrCmp $3 "." rl_safe_done
  StrCmp $3 " " rl_safe_done
  Goto rl_safe_next_char
  rl_safe_check_colon:
  StrCmp $3 ":" 0 rl_safe_next_char
  IntCmp $2 1 rl_safe_next_char rl_safe_done rl_safe_done
  rl_safe_next_char:
  IntOp $2 $2 + 1
  Goto rl_safe_char_loop
  rl_safe_last_component:
  StrCpy $3 $RL_Path 1 -1
  StrCmp $3 "." rl_safe_done
  StrCmp $3 " " rl_safe_done
  StrCpy $1 $RL_CanonicalPath
  rl_safe_parent_loop:
  System::Call 'kernel32::GetFileAttributesW(w r1)i.r2 ?e'
  Pop $3
  IntCmp $2 -1 0 rl_safe_parent_attributes rl_safe_parent_attributes
  IntCmp $3 2 rl_safe_parent_next
  IntCmp $3 3 rl_safe_parent_next
  StrCpy $RL_Error "无法确认程序目录及其父目录的属性，已停止操作。请检查访问权限后重试。"
  Goto rl_safe_done
  rl_safe_parent_attributes:
  IntOp $3 $2 & 0x400
  IntCmp $3 0 rl_safe_parent_next
  StrCpy $RL_Error "目录包含符号链接或目录联接，已停止操作以保护其它位置的文件。"
  Goto rl_safe_done
  rl_safe_parent_next:
  StrCmp $1 "$0" rl_safe_ok
  StrCmp $1 "$0\" rl_safe_ok
  ${GetParent} "$1" $2
  StrCmp $2 "" rl_safe_ok
  StrCmp $2 $1 rl_safe_done
  StrCpy $1 $2
  Goto rl_safe_parent_loop
  rl_safe_ok:
  StrCpy $RL_Result "1"
  StrCpy $RL_Error ""
  rl_safe_done:
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

; Prove list access before enumeration, including an empty directory. This
; avoids treating an inaccessible directory as an empty installation target.
Function ${RL_FUNCTION_PREFIX}RL_CheckDirectoryReadable
  Push $0
  Push $1
  StrCpy $RL_Result "0"
  System::Call 'kernel32::CreateFileW(w "$RL_InspectDirectory", i 1, i 7, p 0, i 3, i 0x02000000, p 0)p.r0 ?e'
  Pop $1
  IntCmp $0 -1 rl_readable_denied
  System::Call 'kernel32::CloseHandle(p r0)i.r1'
  StrCpy $RL_Result "1"
  Goto rl_readable_done
  rl_readable_denied:
  StrCpy $RL_Error "无法完整读取程序目录，已停止操作。请确认目录权限后重试。"
  rl_readable_done:
  Pop $1
  Pop $0
FunctionEnd

; Legacy uninstallers remove their entire directory. Such upgrades are only
; safe when every existing entry belongs to the packaged program. New marked
; installs may contain unknown data, which their precise uninstaller retains.
Function ${RL_FUNCTION_PREFIX}RL_CheckContents
  Push $0
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  Push "$RL_InspectDirectory"
  Push "$RL_InspectRelative"
  Call ${RL_FUNCTION_PREFIX}RL_CheckDirectoryReadable
  StrCmp $RL_Result "1" 0 rl_contents_done
  ClearErrors
  FindFirst $0 $1 "$RL_InspectDirectory\*.*"
  IfErrors rl_contents_empty
  rl_contents_loop:
  StrCmp $1 "" rl_contents_close
  StrCmp $1 "." rl_contents_next
  StrCmp $1 ".." rl_contents_next
  StrCpy $2 "$RL_InspectRelative$1"
  !insertmacro RL_IsOwnedRelativePath $2 $3
  StrCmp $3 "1" rl_contents_owned
  StrCmp $RL_AllowUnknown "1" rl_contents_next
  StrCpy $RL_Result "0"
  StrCpy $RL_Error "旧版本的安装目录中有个人资料或未知文件：$2。旧卸载器会删除整个目录，已停止升级。请先将资料备份并移到安装目录之外，再重试。"
  Goto rl_contents_close
  rl_contents_owned:
  StrCpy $5 "$RL_InspectDirectory\$1"
  System::Call 'kernel32::GetFileAttributesW(w r5)i.r4'
  IntCmp $4 -1 rl_contents_changed
  IntOp $3 $4 & 0x400
  IntCmp $3 0 +4
    StrCpy $RL_Result "0"
    StrCpy $RL_Error "程序文件路径包含符号链接或目录联接，已停止操作。"
    Goto rl_contents_close
  IntOp $3 $4 & 0x10
  IntCmp $3 0 rl_contents_next
  Push "$RL_InspectDirectory"
  Push "$RL_InspectRelative"
  StrCpy $RL_InspectDirectory "$5"
  StrCpy $RL_InspectRelative "$2\"
  Call ${RL_FUNCTION_PREFIX}RL_CheckContents
  Pop $RL_InspectRelative
  Pop $RL_InspectDirectory
  StrCmp $RL_Result "1" rl_contents_next rl_contents_close
  rl_contents_changed:
  StrCpy $RL_Result "0"
  StrCpy $RL_Error "检查时程序目录发生变化，请关闭相关程序后重试。"
  Goto rl_contents_close
  rl_contents_next:
  FindNext $0 $1
  Goto rl_contents_loop
  rl_contents_close:
  FindClose $0
  Goto rl_contents_done
  rl_contents_empty:
  System::Call 'kernel32::GetLastError()i.r3'
  IntCmp $3 2 rl_contents_done
  IntCmp $3 18 rl_contents_done
  StrCpy $RL_Result "0"
  StrCpy $RL_Error "无法完整检查程序目录，已停止操作。请确认目录权限后重试。"
  rl_contents_done:
  Pop $RL_InspectRelative
  Pop $RL_InspectDirectory
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

Function ${RL_FUNCTION_PREFIX}RL_ValidateDirectory
  Push $0
  Push $1
  Push $2
  Call ${RL_FUNCTION_PREFIX}RL_CheckSafePath
  StrCmp $RL_Result "1" 0 rl_directory_done
  StrCpy $RL_Path "$RL_CanonicalPath"
  System::Call 'kernel32::GetFileAttributesW(w "$RL_Path")i.r0 ?e'
  Pop $2
  IntCmp $0 -1 0 rl_directory_attributes rl_directory_attributes
  IntCmp $2 2 rl_directory_ok
  IntCmp $2 3 rl_directory_ok
  StrCpy $RL_Result "0"
  StrCpy $RL_Error "无法确认安装目录的状态，已停止操作。请检查访问权限后重试。"
  Goto rl_directory_done
  rl_directory_attributes:
  IntOp $1 $0 & 0x10
  IntCmp $1 0 rl_directory_not_owned
  StrCpy $RL_AllowUnknown "0"
  IfFileExists "$RL_Path\resources\install-owner.ini" 0 rl_directory_legacy
  ReadINIStr $0 "$RL_Path\resources\install-owner.ini" "Install" "Guid"
  StrCmp $0 "${APP_GUID}" 0 rl_directory_not_owned
  StrCpy $RL_AllowUnknown "1"
  Goto rl_directory_walk
  rl_directory_legacy:
  StrCmp $RL_LegacyAllowed "1" 0 rl_directory_empty_check
  IfFileExists "$RL_Path\${APP_EXECUTABLE_FILENAME}" 0 rl_directory_not_owned
  IfFileExists "$RL_Path\${UNINSTALL_FILENAME}" 0 rl_directory_not_owned
  IfFileExists "$RL_Path\resources\app.asar" 0 rl_directory_not_owned
  Goto rl_directory_walk
  rl_directory_empty_check:
  StrCpy $RL_InspectDirectory "$RL_Path"
  Call ${RL_FUNCTION_PREFIX}RL_CheckDirectoryReadable
  StrCmp $RL_Result "1" 0 rl_directory_done
  ClearErrors
  FindFirst $0 $1 "$RL_Path\*.*"
  IfErrors rl_directory_empty_error
  rl_directory_empty_loop:
  StrCmp $1 "" rl_directory_empty_ok
  StrCmp $1 "." rl_directory_empty_next
  StrCmp $1 ".." rl_directory_empty_next
  FindClose $0
  Goto rl_directory_not_owned
  rl_directory_empty_next:
  FindNext $0 $1
  Goto rl_directory_empty_loop
  rl_directory_empty_ok:
  FindClose $0
  Goto rl_directory_ok
  rl_directory_empty_error:
  System::Call 'kernel32::GetLastError()i.r2'
  IntCmp $2 2 rl_directory_ok
  IntCmp $2 18 rl_directory_ok
  StrCpy $RL_Result "0"
  StrCpy $RL_Error "无法检查安装目录是否为空，已停止操作。"
  Goto rl_directory_done
  rl_directory_walk:
  StrCpy $RL_InspectDirectory "$RL_Path"
  StrCpy $RL_InspectRelative ""
  Call ${RL_FUNCTION_PREFIX}RL_CheckContents
  Goto rl_directory_done
  rl_directory_not_owned:
  StrCpy $RL_Result "0"
  StrCpy $RL_Error "所选目录包含其它文件或不属于本程序。请使用独立的空程序目录，文献库应保存在安装目录之外。"
  Goto rl_directory_done
  rl_directory_ok:
  StrCpy $RL_Result "1"
  StrCpy $RL_Error ""
  rl_directory_done:
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

; A path is passed as child-process environment data, never interpolated into
; PowerShell code. Do not kill running apps or other similarly named folders.
Function ${RL_FUNCTION_PREFIX}RL_CheckNotRunning
  Push $0
  Push $1
  StrCpy $RL_Result "0"
  System::Call 'kernel32::SetEnvironmentVariableW(w "RL_INSTALL_PROCESS_PATH", w "$RL_Path\${APP_EXECUTABLE_FILENAME}")i.r0'
  StrCmp $0 "0" rl_process_unknown
  nsExec::ExecToStack /TIMEOUT=15000 '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command "try { $$main=[IO.Path]::GetFullPath($$env:RL_INSTALL_PROCESS_PATH); $$targets=@($$main,[IO.Path]::Combine([IO.Path]::GetDirectoryName($$main),$\'resources$\',$\'backend$\',$\'research-backend.exe$\')); $$processes=@(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $$_.Name -eq [IO.Path]::GetFileName($$main) -or $$_.Name -eq $\'research-backend.exe$\' }); if ($$processes | Where-Object { -not $$_.ExecutablePath }) { exit 2 }; if ($$processes | Where-Object { $$targets -contains [IO.Path]::GetFullPath($$_.ExecutablePath) }) { exit 3 }; exit 0 } catch { exit 2 }"'
  Pop $0
  Pop $1
  System::Call 'kernel32::SetEnvironmentVariableW(w "RL_INSTALL_PROCESS_PATH", p 0)i.r1'
  StrCmp $0 "0" rl_process_ok
  StrCmp $0 "3" 0 rl_process_unknown
  StrCpy $RL_Error "文献工作台仍在运行。请保存笔记、关闭文献工作台并等待后台任务退出后，再安装或卸载。安装器不会强制结束进程。"
  Goto rl_process_done
  rl_process_unknown:
  StrCpy $RL_Error "无法确认文献工作台是否已退出，已停止安装或卸载。请关闭程序并确认系统 PowerShell 可用后重试。"
  Goto rl_process_done
  rl_process_ok:
  StrCpy $RL_Result "1"
  StrCpy $RL_Error ""
  rl_process_done:
  Pop $1
  Pop $0
FunctionEnd
!ifndef BUILD_UNINSTALLER
  ; electron-builder skips CHECK_APP_RUNNING in a UAC inner process. A
  ; hidden section executes this gate unconditionally before its install
  ; section can launch any legacy recursive uninstaller.
  Section "-Research Library installation safety check"
    !insertmacro RL_InstallPreflight
  SectionEnd
!endif
!macroend

!macro RL_ValidateOldDirectory ROOT
  ReadRegStr $RL_Path ${ROOT} "${INSTALL_REGISTRY_KEY}" "InstallLocation"
  ${If} $RL_Path == ""
    ReadRegStr $0 ${ROOT} "${UNINSTALL_REGISTRY_KEY}" "UninstallString"
    !ifdef UNINSTALL_REGISTRY_KEY_2
      ${If} $0 == ""
        ReadRegStr $0 ${ROOT} "${UNINSTALL_REGISTRY_KEY_2}" "UninstallString"
      ${EndIf}
    !endif
    ${If} $0 != ""
      !insertmacro RL_Stop "旧版本的安装登记不完整，无法安全确认卸载目录。已停止升级，请先修复旧版本安装登记。"
    ${EndIf}
  ${EndIf}
  ${If} $RL_Path != ""
    StrCpy $RL_LegacyAllowed "1"
    Call RL_ValidateDirectory
    ${If} $RL_Result != "1"
      !insertmacro RL_Stop "$RL_Error"
    ${EndIf}
    Call RL_CheckNotRunning
    ${If} $RL_Result != "1"
      !insertmacro RL_Stop "$RL_Error"
    ${EndIf}
  ${EndIf}
!macroend

!macro customInit
  ReadRegStr $RL_OriginalInstallDirectory SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "InstallLocation"
!macroend

!macro RL_InstallPreflight
    ; The template can switch CurrentUser -> all after our first section
    ; during a silent upgrade with both installation scopes present. Inspect
    ; those future legacy targets now, before an elevated inner process can
    ; skip its normal CHECK_APP_RUNNING hook.
    ${If} ${Silent}
      ${If} $installMode != "all"
        ReadRegStr $0 HKLM "${INSTALL_REGISTRY_KEY}" "InstallLocation"
        ${If} $0 != ""
          !ifdef isForCurrentUser
            ${IfNot} ${isForCurrentUser}
          !endif
              !insertmacro RL_ValidateOldDirectory HKLM
              !insertmacro RL_ValidateOldDirectory HKCU
          !ifdef isForCurrentUser
            ${EndIf}
          !endif
        ${EndIf}
      ${EndIf}
    ${EndIf}
    ; Re-read after the user changes install mode, so the original directory
    ; of that scope (not the scope initially selected by onInit) is retained.
    ReadRegStr $0 SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "InstallLocation"
    ${If} $0 != ""
      StrCpy $RL_OriginalInstallDirectory "$0"
    ${EndIf}
    ; Assisted NSIS appends APP_FILENAME even to some existing registered
    ; directories. Retain the original path for an ordinary upgrade.
    ${If} $RL_OriginalInstallDirectory != ""
      GetFullPathName $0 "$RL_OriginalInstallDirectory\${APP_FILENAME}"
      GetFullPathName $1 "$INSTDIR"
      ${If} $0 == $1
        StrCpy $INSTDIR "$RL_OriginalInstallDirectory"
      ${EndIf}
    ${EndIf}
    !insertmacro RL_ValidateOldDirectory SHELL_CONTEXT
    ${If} $installMode == "all"
      !insertmacro RL_ValidateOldDirectory HKCU
    ${EndIf}
    StrCpy $RL_Path "$INSTDIR"
    StrCpy $RL_LegacyAllowed "0"
    ReadRegStr $0 SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "InstallLocation"
    ${If} $0 != ""
      GetFullPathName $0 "$0"
      GetFullPathName $1 "$INSTDIR"
      ${If} $0 == $1
        StrCpy $RL_LegacyAllowed "1"
      ${EndIf}
    ${EndIf}
    Call RL_ValidateDirectory
    ${If} $RL_Result != "1"
      !insertmacro RL_Stop "$RL_Error"
    ${EndIf}
    StrCpy $INSTDIR "$RL_Path"
    Call RL_CheckNotRunning
    ${If} $RL_Result != "1"
      !insertmacro RL_Stop "$RL_Error"
    ${EndIf}
!macroend

!macro customCheckAppRunning
  !ifndef BUILD_UNINSTALLER
    ; Recheck with the final mode/path too. The hidden section protects UAC
    ; inner processes; this hook covers the template's later mode changes.
    !insertmacro RL_InstallPreflight
  !else
    StrCpy $RL_Path "$INSTDIR"
    Call un.RL_CheckNotRunning
  !endif
  ${If} $RL_Result != "1"
    !insertmacro RL_Stop "$RL_Error"
  ${EndIf}
!macroend

!macro customInstall
  ClearErrors
  WriteINIStr "$INSTDIR\resources\install-owner.ini" "Install" "Guid" "${APP_GUID}"
  WriteINIStr "$INSTDIR\resources\install-owner.ini" "Install" "Version" "${VERSION}"
  WriteINIStr "$INSTDIR\resources\install-owner.ini" "Install" "Executable" "${APP_EXECUTABLE_FILENAME}"
  ${If} ${Errors}
    !insertmacro RL_Stop "无法写入安装标记，安装未完成。请检查程序目录的写入权限后重试。"
  ${EndIf}
  WriteRegStr SHELL_CONTEXT "${UNINSTALL_REGISTRY_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr SHELL_CONTEXT "Software\Classes\${RL_PROTOCOL}" "" "URL:文献工作台"
  WriteRegStr SHELL_CONTEXT "Software\Classes\${RL_PROTOCOL}" "URL Protocol" ""
  WriteRegStr SHELL_CONTEXT "Software\Classes\${RL_PROTOCOL}\DefaultIcon" "" '$\"$INSTDIR\${APP_EXECUTABLE_FILENAME}$\",0'
  WriteRegStr SHELL_CONTEXT "Software\Classes\${RL_PROTOCOL}\shell\open\command" "" '$\"$INSTDIR\${APP_EXECUTABLE_FILENAME}$\" $\"%1$\"'
!macroend

!macro RL_RemoveOwnProtocol ROOT
  ReadRegStr $0 ${ROOT} "Software\Classes\${RL_PROTOCOL}\shell\open\command" ""
  ${If} $0 == '$\"$INSTDIR\${APP_EXECUTABLE_FILENAME}$\" $\"%1$\"'
    DeleteRegKey ${ROOT} "Software\Classes\${RL_PROTOCOL}"
  ${EndIf}
!macroend

!macro RL_ValidateUninstallDirectory
  StrCpy $RL_Path "$INSTDIR"
  Call un.RL_CheckSafePath
  ${If} $RL_Result != "1"
    !insertmacro RL_Stop "$RL_Error"
  ${EndIf}
  StrCpy $INSTDIR "$RL_CanonicalPath"
  ReadINIStr $0 "$INSTDIR\resources\install-owner.ini" "Install" "Guid"
  ${If} $0 != "${APP_GUID}"
    !insertmacro RL_Stop "程序目录缺少有效的安装标记，已停止卸载。请重新安装修复安装登记后再卸载。"
  ${EndIf}
  StrCpy $RL_LegacyAllowed "0"
  Call un.RL_ValidateDirectory
  ${If} $RL_Result != "1"
    !insertmacro RL_Stop "$RL_Error"
  ${EndIf}
!macroend

!macro customUnInit
  !insertmacro RL_ValidateUninstallDirectory
  Call un.RL_CheckNotRunning
  ${If} $RL_Result != "1"
    !insertmacro RL_Stop "$RL_Error"
  ${EndIf}
!macroend

!macro customUnInstall
  !insertmacro RL_ValidateUninstallDirectory
!macroend

!macro RL_DeleteProgramFile RELATIVE
  ${If} ${FileExists} "$INSTDIR\${RELATIVE}"
    ClearErrors
    Delete "$INSTDIR\${RELATIVE}"
    ${If} ${Errors}
      !insertmacro RL_Stop "程序文件无法移除：${RELATIVE}。请关闭占用该文件的程序后重试。个人资料不会被清空。"
    ${EndIf}
  ${EndIf}
!macroend

!macro customRemoveFiles
  !insertmacro RL_ValidateUninstallDirectory
  SetOutPath "$TEMP"
  !insertmacro RL_RemoveOwnedProgramFiles
  ; Remove the launcher only after successful program-file removal. A busy
  ; file or invalid marker must leave the existing installation reachable.
  !insertmacro RL_RemoveOwnProtocol SHELL_CONTEXT
  !insertmacro RL_RemoveOwnProtocol HKCU
!macroend
!endif
