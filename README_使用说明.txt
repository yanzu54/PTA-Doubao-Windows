PTA / Doubao - Windows one-click launcher (pip and Playwright fix)

1. Extract the ZIP completely (do not run BAT inside the ZIP).
2. Double-click START_PTA_Doubao.bat.
3. The launcher detects installed Windows Python, then checks pip and Playwright.
4. If pip is broken it tries ensurepip, then recreates .runtime_venv if necessary.
5. If Playwright is missing it installs from a mirror or official PyPI.
6. If setup fails, send run_logs\setup_diagnostics.log (full traceback).
7. When Chrome/Edge is absent it attempts to install Playwright Chromium.

This build addresses the previously truncated Traceback at setup step 3.
It has been statically checked, but not run on your Windows machine.
Use only where AI assistance and automation are permitted.
Compiler/sample checks do not guarantee 95% accuracy or full scores.
