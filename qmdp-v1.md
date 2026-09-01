# QMDP v1 — QEMU–Moonlight Desktop Profile side-channel

## 1. Назначение

QMDP не заменяет GameStream/Moonlight media и input streams. Это необязательное расширение desktop-функций:

- capability negotiation;
- live viewport resize;
- text clipboard;
- local cursor metadata;
- session status/reconnect.

Обычный Moonlight-клиент без QMDP продолжает получать видео, звук и передавать input.

## 2. Transport и authentication

Рекомендуемый transport: WebSocket поверх Sunshine HTTPS listener.

Требования:

- paired client certificate должен быть проверен штатным Sunshine TLS layer;
- клиент передаёт короткоживущий `session_token`, выданный при launch;
- token связан с client certificate fingerprint, VM id и active GameStream session;
- каждое сообщение имеет monotonic `seq`;
- максимальный JSON header — 64 KiB;
- clipboard binary payload может идти отдельным binary frame после metadata.

Endpoint:

```text
GET /api/qmdp/v1/ws?session_token=<opaque>
```

## 3. Envelope

```json
{
  "v": 1,
  "type": "viewport.set",
  "seq": 42,
  "session_id": "opaque-session-id",
  "payload": {}
}
```

Неизвестные optional message types игнорируются с `error.unsupported`; неверная major version отключает QMDP для сессии.

## 4. Handshake

### `hello`

Client → host:

```json
{
  "v": 1,
  "type": "hello",
  "seq": 1,
  "session_id": "...",
  "payload": {
    "client": "moonlight-qt-qmdp",
    "client_version": "0.1.0",
    "features": ["viewport", "clipboard_text", "local_cursor"]
  }
}
```

### `capabilities`

Host → client:

```json
{
  "v": 1,
  "type": "capabilities",
  "seq": 1,
  "session_id": "...",
  "payload": {
    "features": {
      "viewport": true,
      "clipboard_text": true,
      "local_cursor": true,
      "remote_scale": false
    },
    "display": {
      "min_width": 640,
      "min_height": 480,
      "max_width": 7680,
      "max_height": 4320,
      "width_alignment": 2,
      "height_alignment": 2,
      "refresh_millihz": [60000, 120000]
    },
    "clipboard": {
      "max_bytes": 1048576,
      "mime_types": ["text/plain;charset=utf-8"],
      "directions": ["client_to_guest", "guest_to_client"]
    }
  }
}
```

## 5. Viewport

### `viewport.set`

```json
{
  "v": 1,
  "type": "viewport.set",
  "seq": 20,
  "session_id": "...",
  "payload": {
    "request_id": 9001,
    "width": 2560,
    "height": 1440,
    "refresh_millihz": 60000,
    "remote_scale_percent": 125,
    "policy": "follow_window"
  }
}
```

Host rules:

1. validate limits/alignment;
2. coalesce pending requests;
3. apply only latest request after server-side rate limit/debounce;
4. call QEMU `SetUIInfo`;
5. wait for matching/new scanout generation;
6. reconfigure encoder and request IDR;
7. answer with actual mode.

### `viewport.applied`

```json
{
  "v": 1,
  "type": "viewport.applied",
  "seq": 21,
  "session_id": "...",
  "payload": {
    "request_id": 9001,
    "width": 2560,
    "height": 1440,
    "refresh_millihz": 60000,
    "remote_scale_percent": 100,
    "surface_generation": 18
  }
}
```

### `viewport.rejected`

```json
{
  "v": 1,
  "type": "viewport.rejected",
  "seq": 22,
  "session_id": "...",
  "payload": {
    "request_id": 9001,
    "reason": "unsupported_mode",
    "fallback_width": 1920,
    "fallback_height": 1080
  }
}
```

Клиент игнорирует ACK для `request_id`, меньшего последнего отправленного/применённого.

## 6. Clipboard

### `clipboard.offer`

```json
{
  "v": 1,
  "type": "clipboard.offer",
  "seq": 30,
  "session_id": "...",
  "payload": {
    "item_id": "uuid",
    "origin_id": "client-uuid",
    "generation": 71,
    "mime_types": ["text/plain;charset=utf-8"],
    "size": 214,
    "sha256": "hex"
  }
}
```

### `clipboard.request`

```json
{
  "v": 1,
  "type": "clipboard.request",
  "seq": 31,
  "session_id": "...",
  "payload": {
    "item_id": "uuid",
    "mime_type": "text/plain;charset=utf-8"
  }
}
```

### `clipboard.data`

Metadata JSON, затем ровно один binary frame указанного размера:

```json
{
  "v": 1,
  "type": "clipboard.data",
  "seq": 32,
  "session_id": "...",
  "payload": {
    "item_id": "uuid",
    "mime_type": "text/plain;charset=utf-8",
    "size": 214,
    "sha256": "hex"
  }
}
```

### Loop suppression

Получатель запоминает `(origin_id, generation, sha256)` и не публикует тот же item обратно отправителю.

## 7. Cursor

### `cursor.define`

Cursor pixel payload — отдельный binary frame, формат premultiplied ARGB8888.

```json
{
  "v": 1,
  "type": "cursor.define",
  "seq": 40,
  "session_id": "...",
  "payload": {
    "cursor_id": 12,
    "width": 32,
    "height": 32,
    "hotspot_x": 3,
    "hotspot_y": 2,
    "format": "argb8888-premultiplied",
    "size": 4096
  }
}
```

### `cursor.move`

```json
{
  "v": 1,
  "type": "cursor.move",
  "seq": 41,
  "session_id": "...",
  "payload": {
    "cursor_id": 12,
    "x": 1000,
    "y": 650,
    "visible": true,
    "surface_generation": 18
  }
}
```

После подтверждения local cursor host не композитит cursor в video frames. При QMDP disconnect host возвращается к baked cursor после ближайшего IDR.

## 8. Heartbeat и ошибки

- `ping`/`pong` каждые 5 секунд при отсутствии traffic;
- 15 секунд без pong закрывают side-channel, но не обязательно GameStream;
- rate limit viewport: не более 10 входящих событий/с, применяется не чаще 4/с;
- rate limit clipboard offer: 20/мин по умолчанию;
- error payload не включает secret/token/raw clipboard data.
