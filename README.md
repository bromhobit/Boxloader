# Boxloader

**Boxloader** — настольная утилита для подписи и установки iOS-приложений (`.ipa`) по USB из Linux. Авторизация Apple ID с 2FA, работа с iPhone и подпись IPA реализованы через Rust-движок `dopamine-loader-core`. Графический интерфейс — PySide6, запуск — через **скомпилированный Rust ELF**, без запуска shell-скрипта.

## Возможности

- Поиск iPhone через USB и проверка соединения.
- Вход в Apple ID и двухфакторная авторизация.
- Выбор, подпись и установка IPA на iPhone.
- Настройка **Sidebox**: сопряжение устройства и, при наличии, перенос сохранённой сессии Apple ID.
- Журнал операций, наглядные этапы установки и настройка каталогов.

Вкладки **Rebox / Dopamine Renew**, продление подписи и отдельная кнопка отправки pairing-файла в SideStore удалены. Универсальная установка IPA сохраняется; функции SideStore/Sidebox по запросу можно использовать через установку соответствующих IPA. Проект Rebox: https://github.com/bromhobit/Rebox.

## Скачать

Готовые сборки доступны в [GitHub Releases](https://github.com/bromhobit/Boxloader/releases).

| Платформа | Файл | Установка |
|---|---|---|
| **Arch Linux / CachyOS** | `boxloader-0.2.2-1-x86_64.pkg.tar.zst` | `sudo pacman -U ./boxloader-0.2.2-1-x86_64.pkg.tar.zst` |
| **Современный Debian-совместимый Linux (экспериментально)** | `boxloader_0.2.2-1_amd64.deb` | `sudo apt install ./boxloader_0.2.2-1_amd64.deb` |
| **AppImage (экспериментально)** | `Boxloader-0.2.2-x86_64.AppImage` | `chmod +x Boxloader-0.2.2-x86_64.AppImage && ./Boxloader-0.2.2-x86_64.AppImage` |

Arch-пакет протестирован на CachyOS. Пакет DEB требует **Python 3.14** и соответствующий PySide6, поэтому на обычных Ubuntu/Debian установка может быть недоступна. AppImage содержит Python 3.14 и Qt 6, проверен на CachyOS; переносимость на старые дистрибутивы не подтверждена из-за современных системных библиотек. Эти сборки рассчитаны на **Linux x86-64**. Для установки на сам iPhone они не предназначены.

После установки ищите **Boxloader** в меню приложений KDE или запускайте командой `boxloader`.

## Где находятся файлы после установки

- **Исходники:** `~/Проекты/Boxloader/` (Git-репозиторий).
- **Исполняемый файл (пользовательская установка):** `~/.local/bin/boxloader`.
- **Интерфейс:** `~/.local/share/boxloader/app/`.
- **Rust USB-движок:** `~/.local/libexec/boxloader/dopamine-loader-core`.
- **Меню KDE:** `~/.local/share/applications/boxloader.desktop`.
- **Сессия Apple ID:** `~/.local/state/boxloader/apple-session/` (защищённый каталог 0700, файлы 0600).

Никаких рабочих каталогов Codex для запуска не требуется. Данные Apple ID, provisioning-профили и pairing-файлы **не входят в исходники и релизы**. Старые пути могут быть сохранены как символические ссылки исключительно ради совместимости.

## Сборка и тесты

Требуется Rust/Cargo, Python 3.14 с PySide6, Clang/линковщик и работающий `usbmuxd`.

```bash
cargo build --locked --release --manifest-path core/Cargo.toml
cargo build --locked --release --manifest-path native-launcher/Cargo.toml
python3 -m unittest discover -s gui -p 'test_*.py' -q
python3 packaging/build_release.py
python3 packaging/build_appimage.py  # нужен appimagetool
```

Архивы релизов создаются в `dist/`. Скрипты включают только явно разрешённые файлы: GUI, иконку, desktop-entry и скомпилированные бинарники, без файлов из `~/.local/state`, `~/Documents/Codex` и других приватных каталогов.

`boxloader --self-test` проверяет доступность Rust-движка и создание двух вкладок GUI без подключения iPhone. Полная проверка подписи/установки на устройстве выполняется отдельно. Поскольку запускатель скомпонован с `libpython3.14`, после обновления системного Python до следующей minor-версии системный ELF необходимо пересобрать.

## Совместимость с предыдущими версиями

Внутренние имена Rust-движка и CLI (`dopamine-loader-core`) сохранены, а проект, desktop-entry и исполняемый файл имеют название **Boxloader**. Это позволяет обновить интерфейс без изменения старого протокола USB.
