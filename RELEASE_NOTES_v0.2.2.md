## Boxloader 0.2.2

Обновление совместимости с **Sidebox 1.0.0** (`com.bromhobit.Sidebox`).

### Что изменилось
- **Распознавание нового и старого Bundle ID Sidebox (`core` + `gui`):**
  - USB-ядро `dopamine-loader-core` теперь распознаёт новое семейство `com.bromhobit.Sidebox` / `com.bromhobit.Sidebox.<TEAM_ID>` с приоритетом перед legacy `org.codex.SideBox` / `org.codex.SideBox.<TEAM_ID>`, сохраняя полную обратную совместимость при переносе pairing-файла и сессии Apple ID.
  - После установки `Sidebox-1.0.0.ipa` интерфейс корректно предлагает настроить **Sidebox** (pairing + Apple ID).
- **Сборка и пакеты 0.2.2:**
  - Пересобраны системный ELF-лаунчер `boxloader` и ядро `dopamine-loader-core`, обновлены пакеты для Arch Linux / CachyOS, Debian и AppImage.

### Пакеты и SHA-256
- **Arch Linux / CachyOS:** `boxloader-0.2.2-1-x86_64.pkg.tar.zst` (`sudo pacman -U ./boxloader-0.2.2-1-x86_64.pkg.tar.zst`).
- **Debian / Ubuntu (экспериментально):** `boxloader_0.2.2-1_amd64.deb` (требуется Python 3.14 и PySide6).
- **AppImage (экспериментально):** `Boxloader-0.2.2-x86_64.AppImage`.
- `SHA256SUMS` — контрольные суммы SHA-256.
