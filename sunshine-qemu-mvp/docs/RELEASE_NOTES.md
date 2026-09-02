# Релиз: установка, запуск и проверка

Этот документ — шаблон release notes и порядок установки для Proxmox VE 9 и
macOS Tahoe. Он **не утверждает**, что какой-либо DEB/DMG уже опубликован,
подписан Developer ID или нотарифицирован. Перед установкой сверяйте имя
артефакта, SHA-256, источник и статус подписи с доверенным каналом релиза.

Поддерживаемый media-маршрут не использует PIN:

```text
Qt-клиент: login/password -> TLS/PAM authd -> RAM-only VM ticket
  -> patched Moonlight --qsm-system-auth -> короткий mTLS GameStream lease
  -> patched Sunshine -> private QEMU Display1 -> VirGL guest
```

Буфер обмена, ограниченный file transfer и смена guest-разрешения идут через
отдельный ticket-authenticated QSF; это не пакеты GameStream.

## Текст для GitHub Release (заполнить перед публикацией)

Ниже готовая основа тела GitHub Release. Замените квадратные скобки реальными
значениями и удалите пункты, которые не относятся к конкретному релизу.

```markdown
## q-sunshine [VERSION]

Артефакты для проверки:

- `q-sunshine-pve_[VERSION]_amd64.deb` — Proxmox VE 9 server package
- `q-sunshine-[PROJECT_VERSION]+g[REVISION]-macos-tahoe-[ARCH].dmg` — macOS
  Tahoe Qt client с встроенным patched Moonlight
- `SHA256SUMS` — сверить перед установкой

### Что изменилось

- Native system-auth: Qt login/password -> RAM-only ticket -> patched
  Moonlight `--qsm-system-auth` -> ephemeral VM-bound mTLS lease.
- Native Sunshine отключает legacy PIN/pairing для такого instance.
- Windowed/fullscreen, guest resize, clipboard и ограниченный file transfer
  проходят через Qt + QSF/VirGL маршрут.

### Быстрый порядок запуска

1. На Proxmox установить DEB: `sudo apt install ./q-sunshine-pve_[VERSION]_amd64.deb`.
2. Настроить private QEMU Display1, VirGL, guest QSF agent и per-VM auth/lease
   material; см. `docs/RELEASE_NOTES.md` и установленный `README.Debian`.
3. Включить `q-sunshine@VMID`, `q-sunshine-qsf-control@VMID`,
   `q-sunshine-auth@VMID` и `q-sunshine-qsf-system-auth-gateway@VMID`.
4. На macOS проверить DMG, перенести приложение в `/Applications`, создать
   профиль с точными CA/SNI/audience и войти системным login/password.

### Важно

- Не включать одновременно legacy `q-sunshine-qsf-gateway@VMID` и
  ticket-mode `q-sunshine-qsf-system-auth-gateway@VMID`.
- QSF — отдельный companion protocol, не расширение GameStream.
- Signing/notarization status этого релиза: **[ЗАПОЛНИТЬ]**.
```

## Proxmox VE 9: установить и подготовить host

1. Скопируйте проверенный DEB на Proxmox VE 9 и установите через APT:

   ```bash
   sudo apt install ./q-sunshine-pve_*.deb
   sudo q-sunshine-preflight --virgl
   ```

   Установка не включает сервисы, не меняет VM, не создаёт ключи и не открывает
   firewall-порты.

2. Выберите VMID (ниже `100`) и сначала подготовьте её QEMU-процесс. Учетная
   запись, под которой работает QEMU, должна иметь `rw` к `/dev/kvm` и к
   render node (обычно `/dev/dri/renderD128`). VM нужна уникальная private
   Display1 D-Bus точка и VirGL display, эквивалентный:

   ```text
   -device virtio-vga-gl -display dbus,gl=on
   ```

   Не используйте system/session D-Bus и не делите socket между VM.
   Конкретное изменение конфигурации Proxmox/VM остаётся задачей оператора;
   q-sunshine её не редактирует.

3. Для clipboard, files и guest geometry добавьте отдельный private
   virtio-serial server socket для `qsf-guest-agent`. Установите в госте
   supplied QSF agent и VirGL display adapter. Adapter обязан подтвердить
   фактический scanout; одного ответа `Console.SetUIInfo` недостаточно.

## Proxmox VE 9: настроить native system-auth instance

1. Создайте root-only instance файл и внесите private QEMU endpoints:

   ```bash
   sudo install -d -o root -g root -m 0750 /etc/q-sunshine/instances.d
   sudo install -o root -g root -m 0600 \
     /usr/share/doc/q-sunshine-pve/example-instance.conf \
     /etc/q-sunshine/instances.d/100.conf
   sudoedit /etc/q-sunshine/instances.d/100.conf
   ```

   Укажите per-VM Display1 address, render node, отдельный GameStream port,
   QSF agent socket и, при необходимости, проверенный encoder envelope.
   Шаблон по умолчанию безопасно слушает только `127.0.0.1`, поэтому для
   удалённого macOS-клиента его недостаточно. Выберите один конкретный
   защищённый LAN/VPN address (ниже `192.168.64.25` только пример) и не
   публикуйте эти listeners в Internet.

2. Создайте отдельные для VM TLS material, 32-byte auth ticket key и
   GameStream lease CA. Точные X.509 profile, ownership и mode описаны в
   [`SYSTEM_AUTH.md`](SYSTEM_AUTH.md) и
   [`GAMESTREAM_LEASE_AUTH.md`](GAMESTREAM_LEASE_AUTH.md):

   - ticket key и GameStream CA key: regular root-owned non-symlink, `0600`;
   - GameStream CA certificate: абсолютный root-owned regular path, `0644`,
     без group/world write;
   - `QSUNSHINE_AUTH_AUDIENCE` — неизменяемое точное значение, например
     `vm-100`, а не отображаемое имя профиля;
   - не переиспользуйте auth TLS key, QSF TLS key или GameStream CA между VM.

3. Заполните все обязательные группы в
   `/etc/q-sunshine/instances.d/100.conf` реальными абсолютными путями:

   ```ini
   QSUNSHINE_AUTH_SERVER_CERT=/etc/q-sunshine/auth/100/server.crt
   QSUNSHINE_AUTH_SERVER_KEY=/etc/q-sunshine/auth/100/server.key
   QSUNSHINE_AUTH_TICKET_KEY=/etc/q-sunshine/auth/100/ticket.key
   QSUNSHINE_AUTH_AUDIENCE=vm-100
   QSUNSHINE_AUTH_ALLOWED_USERS=alice
   QSUNSHINE_BIND_ADDRESS=192.168.64.25
   QSUNSHINE_PORT=47989
   QSUNSHINE_AUTH_GATEWAY_HOST=192.168.64.25
   QSUNSHINE_AUTH_GATEWAY_PORT=48123

   QSUNSHINE_GAMESTREAM_LEASE_ISSUER=/usr/lib/q-sunshine/bin/q-sunshine-lease-issuer
   QSUNSHINE_GAMESTREAM_LEASE_CA_CERT=/etc/q-sunshine/auth/100/gamestream-ca.crt
   QSUNSHINE_GAMESTREAM_LEASE_CA_KEY=/etc/q-sunshine/auth/100/gamestream-ca.key
   QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_CERT=/var/lib/q-sunshine/100/sunshine/credentials/cacert.pem
   QSUNSHINE_GAMESTREAM_LEASE_TTL_SECONDS=300

   QSUNSHINE_QSF_SERVER_CERT=/etc/q-sunshine/auth/100/qsf-server.crt
   QSUNSHINE_QSF_SERVER_KEY=/etc/q-sunshine/auth/100/qsf-server.key
   QSUNSHINE_QSF_GATEWAY_HOST=192.168.64.25
   QSUNSHINE_QSF_GATEWAY_PORT=48122
   ```

   Все пять `QSUNSHINE_GAMESTREAM_LEASE_*` обязательны вместе. Тогда launcher
   включает native system-auth с тем же CA/audience; частичная конфигурация
   завершается fail-closed, а не PIN fallback. Установите и проверьте
   `/etc/pam.d/q-sunshine-remote` до задания allowlist. Текущий wire protocol
   поддерживает один login/password exchange, а не произвольный interactive MFA.
   `QSUNSHINE_BIND_ADDRESS`, `QSUNSHINE_AUTH_GATEWAY_HOST` и
   `QSUNSHINE_QSF_GATEWAY_HOST` должны быть тем же реально доступным
   защищённым address (или тремя намеренно разными protected addresses); если
   оставить unit defaults `127.0.0.1`, удалённый Mac не сможет подключиться.
   Для default Sunshine base port `47989` ограничьте firewall/VPN только
   trusted clients, но разрешите весь GameStream набор, а не один base port:
   TCP `47984-47990,48010`, UDP `47998-48000`, плюс TLS/PAM TCP `48123` и
   QSF TCP `48122`. При изменении Sunshine base port пересчитайте его
   производные порты по документации Sunshine, а не переносите этот default
   список буквально.

4. После создания QEMU sockets включите сервисы в этом порядке. Сначала
   `q-sunshine@100` создаёт свой default Sunshine leaf в
   `/var/lib/q-sunshine/100/sunshine/credentials/cacert.pem`; убедитесь, что
   именно этот root-owned regular file существует, прежде чем authd начнёт
   выдавать pinned lease:

   ```bash
   sudo systemctl enable --now q-sunshine@100
   sudo test -f /var/lib/q-sunshine/100/sunshine/credentials/cacert.pem
   sudo systemctl enable --now q-sunshine-qsf-control@100
   sudo systemctl enable --now q-sunshine-auth@100
   sudo systemctl enable --now q-sunshine-qsf-system-auth-gateway@100
   ```

   Не включайте одновременно `q-sunshine-qsf-gateway@100` (optional legacy
   client-mTLS для CLI/compatibility) и
   `q-sunshine-qsf-system-auth-gateway@100` (Qt ticket mode).

## macOS Tahoe: установить, войти и подключиться

1. Проверьте DMG из доверенного release channel, смонтируйте его и перенесите
   `q-sunshine-client.app` в `/Applications`. В bundle уже есть обязательный
   patched Moonlight; не подменяйте его установленным Moonlight.app и не
   используйте `PATH` override.

2. На вкладке desktop profile укажите reachable Sunshine host (например
   `192.168.64.25`) и Desktop application. В System authentication укажите
   отдельный reachable endpoint `192.168.64.25:48123`, TLS SNI, CA PEM и
   точный Expected VM audience (например `vm-100`). Пароль и ticket в профиле
   не сохраняются.

3. На вкладке QSF companion отдельно заполните reachable
   `192.168.64.25:48122`, его TLS SNI и CA PEM, затем нажмите **Save** и
   **Test gateway**. Это независимый TLS endpoint: значения с system-auth
   вкладки не копируются автоматически.

4. Войдите разрешённым системным login/password и нажмите **Connect**. В UI
   доступны только `windowed` и `fullscreen`. Child запускается только с
   `--qsm-system-auth`; обычный Moonlight должен fail-closed, а не перейти к
   PIN pairing. После появления видео вручную активируйте QSF для clipboard,
   ограниченного file transfer и guest-size. Для VirGL resolution используйте
   **Choose optimal stream profile**: оно сериализует QSF, retirement capture,
   QEMU SetUIInfo, guest scanout ACK и запуск replacement stream.

### Signing caveat macOS

Dev DMG по умолчанию ad-hoc signed. Developer ID signature можно запросить
при сборке, но notarization — отдельный шаг дистрибьютора. Unsigned,
ad-hoc-signed или unnotarized artifact может блокироваться Gatekeeper. Не
отключайте Gatekeeper глобально: сначала проверьте происхождение/подпись и
следуйте утверждённой локальной или MDM процедуре. Конкретный релиз обязан
явно указывать свой signing/notarization status; этот документ его не заявляет.

## Проверка, срок ticket и удаление

На Proxmox сначала проверьте local preflight и units:

```bash
sudo q-sunshine-preflight --virgl
sudo systemctl status q-sunshine@100 q-sunshine-qsf-control@100 \
  q-sunshine-auth@100 q-sunshine-qsf-system-auth-gateway@100
sudo journalctl -u q-sunshine@100 -u q-sunshine-auth@100 -b
```

На macOS проверьте успешный system login, отсутствие PIN/pairing dialog,
windowed и fullscreen video, guest input, QSF clipboard/files и guest-size
handoff. Development reference: `run.qAMtPU`; его summary содержит native
lease/no-PIN маркеры и все guest data-plane assertions.

Явный logout или смена profile route/trust/audience останавливает managed
Moonlight child и QSF. Обычный expiry ticket снимает local admission и QSF для
будущих действий, но не hard-cut уже допущенный encrypted media stream; новый
launch после expiry требует нового login.

Для удаления одной VM сначала остановите её units, затем при необходимости
удалите пакет:

```bash
sudo systemctl disable --now q-sunshine-qsf-system-auth-gateway@100 \
  q-sunshine-auth@100 q-sunshine-qsf-control@100 q-sunshine@100
sudo apt purge q-sunshine-pve
```

Purge не решает, удалять ли credentials/state. После backup или намеренной
rotation удаляйте только подтверждённые VM-specific paths:
`/etc/q-sunshine/instances.d/100.conf`, `/etc/q-sunshine/auth/100`,
`/etc/q-sunshine/sunshine/100` и `/var/lib/q-sunshine/100`. Не используйте
wildcard и не удаляйте directory другой VM.
