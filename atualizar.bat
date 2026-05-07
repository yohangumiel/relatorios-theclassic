@echo off
setlocal
cd /d "%~dp0"

echo.
echo ========================================
echo   The Classic PW - Atualizar Relatorios
echo ========================================
echo.

:: Detecta Python - verifica caminhos reais antes do alias da Microsoft Store
set PYTHON=

:: Caminhos diretos (Anaconda, Miniconda, instalacao padrao)
for %%P in (
    "%USERPROFILE%\anaconda3\python.exe"
    "%USERPROFILE%\miniconda3\python.exe"
    "%USERPROFILE%\miniforge3\python.exe"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python311\python.exe"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python310\python.exe"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python39\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
) do (
    if not defined PYTHON (
        if exist %%P set PYTHON=%%P
    )
)

:: Fallback: python do PATH, ignorando alias da Microsoft Store
if not defined PYTHON (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PYTHON (
            echo %%i | findstr /i "WindowsApps" >nul || set PYTHON=%%i
        )
    )
)

if not defined PYTHON (
    echo [ERRO] Python nao encontrado.
    echo Caminhos verificados: anaconda3, miniconda3, miniforge3, AppData\Programs\Python
    echo Adicione o Python ao PATH ou ajuste este arquivo com o caminho correto.
    pause
    exit /b 1
)
echo Usando: %PYTHON%
echo.

:: Gera periodos 7d e 30d com Feedback Adm
%PYTHON% export.py --period 7d --feedback --output relatorio_7d.html
if errorlevel 1 ( echo [ERRO] Falha no 7d. & pause & exit /b 1 )

%PYTHON% export.py --period 30d --feedback --output relatorio_30d.html
if errorlevel 1 ( echo [ERRO] Falha no 30d. & pause & exit /b 1 )

:: Verifica se existe repositorio git
git rev-parse --git-dir >nul 2>&1
if errorlevel 1 (
    echo [AVISO] Repositorio git nao encontrado. Relatorios gerados localmente.
    pause
    exit /b 0
)

echo Subindo para GitHub...
git add relatorio_7d.html relatorio_30d.html
:: Adiciona arquivos KDA se existirem
for %%f in (guerra_*.html) do git add "%%f"
git commit -m "relatorio: att %date% %time:~0,5%"
git push

if errorlevel 1 (
    echo.
    echo [AVISO] Falha no git push. Os arquivos foram gerados mas nao enviados.
) else (
    echo.
    echo Pronto! Relatorios publicados.
    echo Acesse sua pagina do GitHub Pages para ver o resultado.
)

echo.
pause
