; SmartAssistant NSIS customization (RC3 tasks F3/F4).
; Wired in via package.json -> build.nsis.include.
; Contract: the per-user install directory is %LOCALAPPDATA%\Programs\
; smart-assistant-desktop (driven by the package.json `name`; productName is
; display-only). These hooks NEVER touch %USERPROFILE%\.cow or any user data.

!macro customInit
  ; F4: clean the empty rc.1-era shell directory left behind by the legacy
  ; installer layout (%LOCALAPPDATA%\Programs\SmartAssistant). Only an
  ; EMPTY directory is removed; if any file or subdirectory survives there,
  ; the directory is left untouched for manual inspection.
  Push $R0
  Push $R1
  Push $R2
  StrCpy $R0 "$LOCALAPPDATA\Programs\SmartAssistant"
  IfFileExists "$R0\." rc3_rc1_exists rc3_rc1_done
  rc3_rc1_exists:
    FindFirst $R1 $R2 "$R0\*"
    StrCmp $R2 "" rc3_rc1_empty rc3_rc1_close
  rc3_rc1_empty:
    RMDir "$R0"
    Goto rc3_rc1_done
  rc3_rc1_close:
    FindClose $R1
  rc3_rc1_done:
  Pop $R2
  Pop $R1
  Pop $R0
!macroend

!macro customUnInstall
  ; F3: the running uninstaller copy lives inside $INSTDIR and NSIS cannot
  ; delete the directory that hosts it. Schedule a detached, delayed removal
  ; so the empty root directory does not survive the uninstall.
  Exec 'cmd.exe /c timeout /t 2 /nobreak >nul & rmdir /s /q "$INSTDIR"'
!macroend
