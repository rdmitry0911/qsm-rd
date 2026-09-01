# Итог инженерного раунда 0.6.0

Дата: 2026-08-31.

## Результат

В этом раунде была выполнена реальная попытка установить `qemu-system-*`,
подготовлен полностью автономный real-QEMU/TCG тест без GPU и закрыт следующий
наиболее важный **доступный** пользовательский слой: непрерывность удалённой
сессии при остановке, перезагрузке или полной замене процесса QEMU вместе с
безопасным сбросом ввода.

Самый важный оставшийся продуктовый слой — сборка адаптера внутри закреплённой
ревизии Sunshine и подключение обычного Moonlight-клиента. Он не был ложно
объявлен закрытым: в изолированной среде нет исходного дерева Sunshine и нет
доступного сетевого канала для его получения.

## Попытка установки и real-QEMU gate

Запущен проектный установщик:

```bash
./scripts/install-qemu-debian.sh
```

Он требует CPU/TCG-capable x86 QEMU и модуль D-Bus display:

```text
qemu-system-x86
qemu-system-modules-opengl  # Debian 13
# or qemu-system-gui     # compatible Ubuntu package layout
qemu-utils
seabios
dbus-daemon
binutils
ffmpeg
```

Фактический результат этой среды:

```text
apt-get update: Temporary failure resolving 'deb.debian.org'
install script exit code: 100
qemu-system-x86_64 after attempt: absent
```

Поэтому запуск настоящего `qemu-system-x86_64` здесь не состоялся, и real-QEMU
критерий остаётся открытым. Ограничение зафиксировано в логах, а не подменено
fake-QEMU результатом.

Готовый тест после появления бинарника:

```bash
./scripts/run-real-qemu-selftest.sh
```

Он собирает 512-байтный BIOS/VGA boot sector, помещает его в стандартный
1.44-MiB floppy image, запускает QEMU под TCG с `-display dbus,gl=off`, держит
один probe-процесс и дважды полностью заменяет QEMU. Не нужны GPU, KVM, ISO,
диск гостя, сеть гостя или графический сервер хоста.

## Закрытый слой: lifecycle resilience и input safety

Добавлен `ResilientQemuDisplay`, который держит стабильную границу для
Sunshine/QemuSource поверх сменяемых QEMU Display1 transports:

```text
стабильная удалённая сессия
  → QEMU generation 1
  → обрыв listener / завершение QEMU
  → синтетический отпуск удерживаемых клавиш и кнопок
  → очистка старых video/audio queues
  → bounded exponential backoff
  → QEMU generation 2
  → повторная регистрация listener
  → повтор последнего SetUIInfo
  → новая namespace surface generation
  → capture reinit + IDR
  → продолжение той же внешней сессии
```

Реализовано:

- bounded exponential reconnect;
- различение обычной processing error и fatal transport disconnect;
- отказ от очереди ввода во время отсутствия QEMU — события отбрасываются и
  никогда не воспроизводятся после reconnect;
- учёт удерживаемых keyboard keys и mouse buttons;
- best-effort `release_all_input()` до teardown каждой transport-generation;
- сохранение и повтор только последнего viewport request;
- отбрасывание поздних frame/audio callbacks от старой generation;
- очистка frame mailbox и audio FIFO при разрыве;
- глобально монотонная namespace generation для surface resources;
- IDR после первого кадра восстановленной generation;
- запрет reconnect-конфигурации для одноразового inherited peer FD.

## Проверка

Финальные результаты:

| Матрица | Результат |
|---|---:|
| Clean RelWithDebInfo CTest | 10/10, 13.68 s |
| ASan + UBSan + LeakSanitizer | 10/10, 13.84 s |
| TSan reconnect concurrency test | 1/1, 0.34 s |
| Deterministic in-process reconnect stress | 100/100 reconnects |
| Repeated cross-process replacement | 10/10 запусков, 30 fake-QEMU processes |
| Shell syntax / YAML / TOML | PASS |
| VGA boot sector and floppy construction | PASS, signature `55 aa` |
| Real `qemu-system-x86_64` execution | NOT RUN: binary unavailable after blocked install |

100-reconnect invariant:

```text
connections=101
fatal disconnects=100
reconnects=100
release-all calls=101
remapped frame generations=101
```

Каждый из десяти независимых межпроцессных прогонов создал три последовательных
владельца `org.qemu`; probe получил три successful connections, два reconnects,
три disconnect callbacks и ноль session errors.

## Приоритет оставшихся слоёв

| Приоритет | Слой | Статус |
|---:|---|---|
| 1 | Полная регистрация в исходном дереве Sunshine + stock Moonlight stream | Главная следующая продуктовая задача; заблокирована отсутствием Sunshine source tree в этой среде |
| 2 | QEMU reconnect + input safety | **Закрыт в 0.6.0** |
| 3 | Один supervised Sunshine worker на ВМ | Не начат; lifecycle-основа теперь готова |
| 4 | Надёжная стартовая геометрия и client-driven live resize/DPI | Частично: `SetUIInfo`, generation reinit и replay готовы |
| 5 | Двусторонний UTF-8 clipboard | Не начат |
| 6 | Локальный cursor на Moonlight-клиенте | Host metadata готова |
| 7 | Unicode/IME desktop text channel | Не начат |
| 8 | DMA-BUF и native hardware encoder | Отложенная оптимизация, не блокирует CPU MVP |
| 9 | Files, microphone и multi-monitor | Не начат |

Проверка с настоящим QEMU/TCG вынесена из продуктового рейтинга: это обязательный
сквозной qualification gate. Harness готов, но запуск в этой среде заблокирован
отсутствием устанавливаемого пакета.

## Основные доказательства

```text
artifacts/validation/lifecycle-0.6.0/release-ctest-verify.log
artifacts/validation/lifecycle-0.6.0/asan-ubsan-ctest-verify.log
artifacts/validation/resilience/tsan-resilience-verify.log
artifacts/validation/resilience/unit-100-reconnect-verify.log
artifacts/validation/resilience/reconnect-release-repeat-10-final.log
artifacts/validation/resilience/message-bus-reconnect-current.log
artifacts/validation/real-qemu-install/install-attempt-current.log
artifacts/validation/real-qemu-install/install-attempt-current.rc
artifacts/validation/real-qemu/selftest-attempt-current.log
```
