@echo off
:: The terminal clip: Grafana waking the agents, in a real console.
::
:: A classic console (conhost) rather than Windows Terminal, because Windows
:: Terminal renders through DirectX and gdigrab captures that as a black
:: rectangle. This one draws with GDI, which is exactly what the capture reads.
::
:: The window sizes itself and then waits, because `mode con` RESIZES it and a
:: capture already attached dies with "Failed to capture image (error 8)" the
:: moment that happens. Everything worth filming is well after the resize.
title ContinuityDemo
color 0F
mode con: cols=118 lines=30
cls
echo.
echo   Grafana fires the alert. This is the receiver accepting it.
echo.
timeout /t 9 /nobreak >nul
gcloud run services logs read continuity-wake --project=grafana-508011 --region=asia-south1 --limit=12
echo.
timeout /t 16 /nobreak >nul
exit
