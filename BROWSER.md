# Работа с браузером

В Antiagent 0.5.0 инструменты `antigravity_agent_spawn`,
`antigravity_agent_followup` и `antigravity_cli_execute` принимают
`browser_mode`:

| Значение | Поведение |
| --- | --- |
| `disabled` | По умолчанию; браузерный сервер не предоставляет инструментов |
| `isolated` | Новый временный профиль Chrome без пользовательских логинов |
| `user_session` | Подключение к существующей сессии Chrome с подтверждением пользователя |

Каждый followup снова требует явного выбора режима. `mode=plan` регулирует
работу с файлами, а не делает браузер read-only: клики и заполнение форм меняют
состояние страницы. Браузерные задачи получают exclusive workspace lock;
`user_session` дополнительно допускает только один cooperating bridge на
пользователя между всеми workspace. Занятая сессия выдаёт ошибку, а не запускает
второго управляющего агента.

## Установка и регистрация

Сначала активируйте новую версию по [POST_UPDATE_ACTIVATION.md](POST_UPDATE_ACTIVATION.md).
Нужны Node.js LTS и Chrome. Установите проверенную версию backend отдельно:

```powershell
npm.cmd install --prefix "$env:LOCALAPPDATA/antiagent/browser-tools" --ignore-scripts --no-audit --no-fund chrome-devtools-mcp@1.8.0
antiagent-browser --print-config --node "C:/Program Files/nodejs/node.exe" --script "$env:LOCALAPPDATA/antiagent/browser-tools/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js"
```

Для другой установки Node укажите его фактический абсолютный путь.
`--print-config` только печатает JSON. Добавьте выведенную запись
`mcpServers.antiagent_browser` в существующий
`~/.gemini/config/mcp_config.json`, сохранив остальные серверы, либо зарегистрируйте
её через `agy mcp add`. Установленный launcher печатает абсолютный путь Python
своего окружения и запускает модуль через `-I`; это исключает импорт одноимённого
модуля из целевого проекта. Используйте установленную команду, а не Python
неустановленного checkout. Не добавляйте `ANTIAGENT_BROWSER_MODE` в `env` записи:
режим передаёт Antiagent отдельно для каждого запуска.

Прямая регистрация имеет вид (пути возьмите из выведенного JSON):

```powershell
agy mcp add antiagent_browser "<ABSOLUTE-PYTHON>" -I -m antiagent_browser --node "<ABSOLUTE-NODE>" --script "<ABSOLUTE-BACKEND-SCRIPT>"
```

Регистрация сервера в Codex не регистрирует его в Antigravity. Настройки
Antigravity описаны в [официальной документации MCP](https://antigravity.google/docs/mcp).
Не переопределяйте эту запись в недоверенном проекте.

## Разрешения и пользовательский вход

В `agy` через `/permissions` настройте необходимые инструменты для вашей задачи.
Для smoke достаточно `mcp(antiagent_browser/new_page)`,
`mcp(antiagent_browser/take_snapshot)`, `mcp(antiagent_browser/close_page)`.
Постоянные правила находятся в `permissions.allow` файла
`~/.gemini/antigravity-cli/settings.json`; добавляйте конкретные правила,
сохраняя остальные настройки. `ask`/`deny` имеют приоритет над `allow`.
Не используйте `--dangerously-skip-permissions`. В headless-режиме незаданные
разрешения могут приводить к `permission_denied`; исправьте конкретное правило
интерактивно. [Документация разрешений](https://antigravity.google/docs/cli/permissions/).

Для `user_session` откройте в Chrome 144+ страницу
`chrome://inspect/#remote-debugging`, включите удалённую отладку и подтвердите
подключение, когда Chrome покажет запрос. Вход на сайт выполните самостоятельно
в браузере. Antiagent не принимает пароль, cookies, bearer token, путь профиля
или произвольный CDP endpoint. Credentials остаются в Chrome; агент получает
содержимое страниц и может действовать с правами текущей сессии. Это не обещание
скрыть от модели персональные данные, отображаемые самим сайтом.
[Подключение к Chrome](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/advanced-usage.md).

Адаптер предоставляет ограниченный список инструментов навигации, снимков,
кликов и заполнения форм. Он блокирует JavaScript evaluation, injected scripts,
network inspection, загрузку файлов и прямые переходы на URL кроме HTTP(S).
Снимки возвращаются через MCP, сохранение в произвольный файл запрещено.
Usage statistics и CrUX выключены. Содержимое разрешённых страниц всё равно
передаётся агенту. Ограничения prompt не заменяют sandbox; штатные ограничения
CLI (`file_scope_enforced=false`, `shell_denied=false`) остаются в силе.

## Пример вызова и проверки

```json
{
  "task": "Открой https://example.com через antiagent_browser, прочитай заголовок и верни его. Не изменяй файлы.",
  "thinking_level": "high",
  "mode": "plan",
  "browser_mode": "isolated"
}
```

Это аргументы `antigravity_agent_spawn`. Дождитесь terminal state через bounded
wait/status и проверьте результат. Для работы в выбранном аккаунте явно замените
`isolated` на `user_session` только после пользовательского разрешения.

Локальный smoke адаптера (не подменяет live smoke через установленный Antiagent):

```powershell
.\.venv\Scripts\python.exe smoke_browser.py
.\.venv\Scripts\python.exe smoke_browser.py --mode isolated --node "<ABSOLUTE-NODE>" --script "<ABSOLUTE-BACKEND-SCRIPT>" --live
```

Второй тест ограничен 50 секундами: создаёт синтетическую локальную страницу,
открывает её в новом профиле, проверяет уникальный маркер и закрывает вкладку.
Ожидаются `live=true`, `marker_found=true`. `--mode user_session --live` выполняет
тот же тест в пользовательской сессии и требует её явного предоставления;
содержимое чужих вкладок скрипт не выводит. Не включайте этот режим в CI.
