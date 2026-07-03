ONVIF проверка времени камер
============================

Назначение:
  Скрипт только читает ONVIF-настройки времени. Он ничего не записывает в камеры.
  Используется для проверки, можно ли менять NTP/часовой пояс через ONVIF.

Тест на одной камере 10.53.240.136:
  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\probe_onvif_time.py" --start-ip 10.53.240.136 --end-ip 10.53.240.136 --credential Admin:1234

Проверка диапазона:
  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\probe_onvif_time.py" --start-ip 10.53.240.30 --end-ip 10.53.240.176 --credential Admin:1234

Важно:
  Запускай только с одним правильным аккаунтом. Не указывай несколько логинов подряд,
  чтобы не получить UserLocked.

Результат:
  outputs\onvif_time_probe.csv
  outputs\onvif_supported.csv
  outputs\onvif_refused.csv

Колонка ok:
  1 - ONVIF время прочитано.
  0 - ONVIF не ответил, порт/путь другой, ONVIF выключен или логин не подошел.
