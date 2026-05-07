@echo off
setlocal
cd /d "%~dp0"

echo.
echo ========================================
echo   The Classic PW - Publicar Arquivos
echo ========================================
echo.

git rev-parse --git-dir >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Repositorio git nao encontrado.
    pause
    exit /b 1
)

echo Subindo arquivos para GitHub...
git add relatorio_7d.html relatorio_30d.html index.html
for %%f in (guerra_*.html) do git add "%%f"
git commit -m "att: %date% %time:~0,5%"
git push

if errorlevel 1 (
    echo.
    echo [AVISO] Falha no git push.
) else (
    echo.
    echo Pronto! Publicado no GitHub Pages.
)

echo.
pause
