# Модель угроз

## 1. Assets

- экран и звук ВМ;
- ввод пользователя;
- clipboard;
- paired Moonlight credentials;
- QEMU control/input surface;
- DMA-BUF file descriptors;
- файлы конфигурации и сертификаты каждого Sunshine instance.

## 2. Trust boundaries

```text
Remote client
    │ encrypted/authenticated Sunshine connection
    ▼
Sunshine per-VM worker
    │ private local Unix D-Bus
    ▼
QEMU process
    │ virtual devices / optional guest integration
    ▼
Guest OS
```

Host kernel/GPU driver и QEMU/Sunshine worker входят в trusted computing base. Guest OS не считается доверенной относительно хоста.

## 3. Обязательные меры MVP

### Local D-Bus

- отдельный private bus/socket на ВМ;
- Unix permissions `0600` или эквивалентная service-user isolation;
- никакого TCP listener;
- Sunshine и QEMU работают под минимально необходимыми правами;
- D-Bus address не попадает в world-readable command line/config.

### Process isolation

- отдельный Unix user или systemd DynamicUser/credentials design после проверки доступа к `/dev/dri`;
- `NoNewPrivileges=true`;
- ограниченный filesystem namespace;
- доступ только к нужным render nodes и VM socket;
- resource limits по FD/RSS/CPU;
- отдельные cert/config directories.

### Input

- input method allowlist, а не произвольный D-Bus proxy;
- endpoint привязан к одной VM/console;
- release-all при disconnect;
- remote SAS/secure-attention операции добавляются только отдельным permission.

### DMA-BUF

- проверить dimensions, plane count, strides, offsets, fourcc и overflow до import;
- ограничить максимальную площадь кадра;
- дублировать FD и закрывать через RAII;
- не доверять modifier/fourcc без allowlist/import validation;
- не логировать raw frame content.

### Clipboard MVP-1

- выключен по умолчанию;
- directions `off`, `client_to_guest`, `guest_to_client`, `bidirectional`;
- максимум 1 MiB для text;
- payload отдаётся по request, а не автоматически всем paired clients;
- foreground/current-session binding;
- origin/generation/hash loop suppression;
- clipboard metadata очищается при disconnect;
- никакого file payload в text channel.

### Side-channel

- TLS 1.3 и обязательный client certificate у удалённого QSF gateway;
- host-local token остаётся в `0600` file и читается gateway; он не уходит к
  Qt/Moonlight client;
- endpoint/certificate settings изолированы по сохранённому desktop profile и
  блокируются на время активного QSF companion;
- Qt client требует явного включения QSF только после визуального подтверждения
  Moonlight stream и сразу отменяет операции при teardown/reconnect;
- **текущее ограничение:** QSF request не имеет криптографической привязки к
  GameStream session id. Gateway должен быть доверенным и выделенным для одной
  VM; не следует подменять это свойство TLS-аутентификацией;
- session-id binding, replay sequence/request id и rate limits — требования
  следующей версии протокола, а не свойства текущего QSF gateway.

## 4. Не включать в MVP

- произвольное выполнение host commands;
- generic D-Bus forwarding клиенту;
- host filesystem browsing;
- автоматическое монтирование client drives;
- shared clipboard между несколькими клиентами;
- multi-user guest login orchestration;
- privilege escalation helper без строго типизированного API.

## 5. Аудит

Логируются события, но не содержимое:

- pairing/connection/disconnection;
- VM/session binding;
- resize metadata;
- clipboard direction/type/size/hash prefix, без payload;
- D-Bus reconnect;
- unsupported frame format;
- fallback reason;
- permission denial.
