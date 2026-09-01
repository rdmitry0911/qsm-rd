# Валидация артефакта

Проверено в контейнере с GNU C++ 14.2.0:

```text
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2
ctest --test-dir build --output-on-failure
./build/qmdp_demo
```

Результат unit tests:

```text
100% tests passed, 0 tests failed out of 1
```

Один фактический запуск симуляции:

```text
Sunshine-QEMU MVP core simulation
  session: idle
  frames published/encoded/dropped: 181/89/91
  encoded audio frames: 71520
  audio FIFO dropped frames: 0
  resize requests superseded: 2
  resulting mode: 2560x1440
  encoder IDR requests: 1
  QEMU input events: 5
```

Числа кадров зависят от scheduler и не являются benchmark. Проверяемые инварианты:

- encoder получает последнюю доступную frame, а не очередь устаревших кадров;
- burst из трёх resize даёт два superseded requests и один фактический mode;
- geometry выравнивается до `2560×1440`;
- mode switch даёт один IDR;
- state machine возвращается в `idle`;
- input events идут через QEMU adapter boundary.

Не проверено этим контейнером:

- реальный QEMU D-Bus ABI;
- реальный DMA-BUF import;
- конкретный EGL modifier;
- VAAPI/NVENC/Vulkan encode;
- Moonlight networking;
- фактическая input-to-photon latency.

Эти пункты являются обязательными gates реального MVP, а не свойствами mock-среды.
