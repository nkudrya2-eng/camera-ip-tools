ONVIF запись NTP и часового пояса
=================================

Назначение:
  Скрипт пишет через ONVIF:
  - NTP-сервер 10.99.200.60
  - режим времени NTP
  - часовой пояс +08:00

Важно:
  Без --apply скрипт ничего не пишет. Это проверочный режим.
  Запускай только с одним правильным аккаунтом.

Проверка без записи на камере 10.53.240.136:
  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\set_onvif_time.py" --start-ip 10.53.240.136 --end-ip 10.53.240.136 --credential Admin:1234 --ntp-server 10.99.200.60 --timezone +08:00

Реальная запись на камере 10.53.240.136:
  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\set_onvif_time.py" --start-ip 10.53.240.136 --end-ip 10.53.240.136 --credential Admin:1234 --ntp-server 10.99.200.60 --timezone +08:00 --apply

Запись на весь диапазон:
  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\set_onvif_time.py" --start-ip 10.53.240.30 --end-ip 10.53.240.176 --credential Admin:1234 --ntp-server 10.99.200.60 --timezone +08:00 --apply

Если камера дает HTTP 500 на NTP:
  Скрипт все равно пробует записать часовой пояс и потом проверяет фактические значения.
  В CSV смотри колонки ntp_write и timezone_write.

Если NTP уже правильный и нужно только поправить часовой пояс:
  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\set_onvif_time.py" --start-ip 10.53.240.30 --end-ip 10.53.240.176 --credential Admin:1234 --ntp-server 10.99.200.60 --timezone +08:00 --skip-ntp --apply

Пояснение по часовому поясу:
  В ONVIF многие камеры используют POSIX-формат.
  Для человеческого +08:00 скрипт отправляет ONVIF TZ: UTC-08:00:00.

Результат:
  outputs\onvif_time_set_results.csv
