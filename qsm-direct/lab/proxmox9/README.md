# PVE 9 laboratory checks

Эти инструменты работают с реальным PVE 9, QEMU Display1 и браузерным WebRTC
peer. Они не устанавливают runtime-пакеты автоматически и не требуют
клиентского приложения.

- `qualify-pve-direct-browser-e2e.cjs` проверяет защищённый same-origin
  browser transport.
- `qualify-pve-display1-ui.cjs` проверяет поля Display1 в реальном диалоге
  Hardware → Display.
- `qualify-qsm-direct-worker-e2e.py` и `qualify-qsm-direct-worker-media-e2e.py`
  проверяют worker против настоящего QEMU Display1.
- `run-qsm-direct-stress-suite.py` — release gate для video, input, resize,
  fullscreen, clipboard, reconnect и нескольких viewers.

Для точного UI-регресса создайте временную PVE-учётную запись только с
`PVEVMUser` на тестовой VM, войдите в UI через браузер и выберите левый пункт
**Console**. Условие успеха: в Console card ровно один QSM iframe, а число
вызовов `window.open` равно нулю.
