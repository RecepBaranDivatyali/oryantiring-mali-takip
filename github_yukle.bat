@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo GitHub deposuna yukleniyor...
git branch -M main
git remote remove origin 2>nul
git remote add origin https://github.com/RecepBaranDivatyali/oryantiring-mali-takip.git
git push -u origin main
echo.
echo Islem tamamlandi! Simdi GitHub sayfasini yenileyebilirsiniz.
pause
