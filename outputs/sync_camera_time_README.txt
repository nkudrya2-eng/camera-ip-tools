Синхронизация времени камер
===========================

Назначение:
  Скрипт проходит по диапазону IP, пингует камеры и пытается включить NTP.
  В режиме --api auto пробует методы по очереди:
  1) ONVIF
  2) Sunell CGI
  NTP-сервер по умолчанию: 10.99.200.60
  Часовой пояс по умолчанию: +08:00

Проверочный запуск без записи в камеры:
  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\sync_camera_time.py" --start-ip 10.53.240.30 --end-ip 10.53.240.176 --ntp-server 10.99.200.60 --timezone +08:00 --dry-run

Диагностика одной камеры без записи настроек:
  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\sync_camera_time.py" --username Admin --start-ip 10.53.240.37 --end-ip 10.53.240.37 --ntp-server 10.99.200.60 --timezone +08:00 --probe-only

Реальный запуск с одним паролем:
  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\sync_camera_time.py" --username Admin --start-ip 10.53.240.30 --end-ip 10.53.240.176 --ntp-server 10.99.200.60 --timezone +08:00

Реальный запуск с несколькими логинами/паролями:
  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\sync_camera_time.py" --start-ip 10.53.240.30 --end-ip 10.53.240.176 --ntp-server 10.99.200.60 --timezone +08:00 --credential Admin:ПАРОЛЬ1 --credential admin:ПАРОЛЬ2

Повторный запуск только по выбранным IP:
  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\sync_camera_time.py" --ip-list 10.53.240.30,10.53.240.32,10.53.240.34 --ntp-server 10.99.200.60 --timezone +08:00 --credential Admin:ПАРОЛЬ1 --credential admin:ПАРОЛЬ2

Подробный вывод по каждому методу:
  Добавь --verbose, если нужно видеть почему ONVIF/Sunell отказали.

Результат:
  outputs\time_sync_results.csv

Колонка ok:
  1 - камера приняла настройку
  0 - камера не ответила, пароль не подошел или API времени не поддерживается

Важно:
  Не запускай много неправильных паролей подряд: камеры могут заблокировать аккаунт.
  Для камер Evidence/Sunell подтвержден формат:
    type=NTP&enableFlag=1&IPProtoVer=1&NTPIP=10.99.200.60&NTPPort=123&NTPCheckTime=3600
  Также скрипт пробует ONVIF и несколько запасных вариантов Sunell/cgi-bin/param.cgi.
  Если конкретная прошивка использует другой API времени, строка будет записана в CSV как ошибка.

Часовой пояс:
  Для Иркутск/Бурятия обычно указывать --timezone +08:00.
  Для Чита/Забайкалье обычно указывать --timezone +09:00.
