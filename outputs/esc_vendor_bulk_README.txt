Массовый поиск и смена IP камер Evidence/Sunell
================================================

Что делает инструмент
---------------------
1. Ищет камеры по Sunell/Evidence, ONVIF WS-Discovery и всем встроенным методам ESC:
   Milesight, TopView, Hunt, Sunell, UNV и DynaColor.
2. Видит камеры с адресами из другой подсети и камеры с одинаковым IP.
3. Создает CSV со следующими полями:
   current_ip, new_ip, model, mac, device_id, serial_number, firmware, mask, gateway, dns.
4. Формирует последовательный план адресов.
5. Передает Sunell-команды через 32-битный NvdcNetSDK.dll, а DynaColor-команды через фирменный broadcast-протокол.

В графическом интерфейсе поиск по заданному IP-диапазону дополнительно проверяет
ответившие адреса прямым авторизованным ONVIF GetDeviceInformation. Это резервный
путь для сетей, где камера доступна по IP, но не отвечает на discovery-пакеты.

ONVIF-only камеры добавляются в список с selected=0. Для чтения MAC, серийного
номера и прошивки через ONVIF потребуется отдельный опрос с логином и паролем.

Оригинальная программа ESC и файлы в Program Files не изменяются.
Пароль в CSV не сохраняется.

1. Поиск камер на объекте
------------------------
Автоматический выбор сетевого интерфейса:

  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\esc_vendor_bulk.py" scan

Если у компьютера несколько сетевых карт, лучше явно указать адрес карты видеонаблюдения:

  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\esc_vendor_bulk.py" scan --interface-ip 10.53.240.253 --timeout 6

Результат:
  outputs\vendor_camera_inventory.csv

2. Формирование плана
---------------------
Пример для пула 10.53.240.30-10.53.240.176:

  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\esc_vendor_bulk.py" assign --start-ip 10.53.240.30 --end-ip 10.53.240.176 --mask 255.255.252.0 --gateway 10.53.240.1 --dns 10.70.200.5

Результат:
  outputs\vendor_camera_plan.csv

Перед применением открой CSV в Excel:
- selected=1 означает, что строка будет обработана;
- selected=0 исключает камеру;
- new_ip можно исправить вручную;
- новые IP не должны повторяться.

3. Проверка плана без отправки команд
------------------------------------

  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\esc_vendor_bulk.py" apply --credential Admin:PASSWORD

Без --apply выводится только план. Камеры не изменяются.

4. Реальное массовое изменение
------------------------------

  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\esc_vendor_bulk.py" apply --credential Admin:PASSWORD --workers 3 --verify-seconds 30 --apply

Перед отправкой скрипт проверяет, что новые IP не повторяются и не отвечают на ping.
Результат записывается в:
  outputs\vendor_camera_results.csv

Статусы:
- verified: новый IP ответил;
- sent_not_verified: SDK принял команду, но новый IP не ответил за отведенное время;
- failed: мост или SDK вернул ошибку.

5. Получение списка из старого pcap
----------------------------------

  py -3 "C:\Users\n.kudrya\Documents\Codex\2026-06-25\new-chat\outputs\esc_vendor_bulk.py" scan-pcap --pcap "C:\Users\n.kudrya\Documents\Codex\1.pcapng"

Это офлайн-режим: сеть и камеры не используются.

Техническая основа
------------------
- поиск Sunell/Evidence: multicast 234.5.6.7, запрос UDP/31001, ответы UDP/31002;
- ответ содержит ProductModel, MACAddr, DeviceId, DeviceIP, SN и SoftWareInfo;
- смена сети: функция CMS_DeviceModifyer_SetHostNetwork из NvdcNetSDK.dll;
- команда адресуется по DeviceId, поэтому старый IP может быть чужим, недоступным или задублированным.

6. Проверка текущего NTP и часового пояса
----------------------------------------

  py -3 esc_vendor_bulk.py audit-time --credential Admin:PASSWORD --credential-for 10.53.240.123-10.53.240.125=admin:OTHER_PASSWORD

Команда ничего не записывает в камеры. Результат сохраняется в
camera_time_audit.csv.
