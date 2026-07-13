#!/usr/bin/env python3
"""
RNS <-> MeshCore Bridge - v.1.0
"""
import asyncio
import subprocess
import time
import sys
import os
import threading
import configparser
import re
import datetime
import json
from meshcore import MeshCore, SerialConnection, EventType

os.environ['RNS_LOG_LEVEL'] = '0'
import RNS, LXMF

GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
RED = "\033[91m"
BLUE = "\033[94m"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_config.ini")


def load_config():
    c = configparser.ConfigParser()
    if os.path.exists(CONFIG_FILE):
        c.read(CONFIG_FILE, encoding='utf-8')
        if 'bridge' in c:
            news_val = c.get('bridge', 'news', fallback='off').lower().strip()
            return {
                'port': c.get('bridge', 'port', fallback='/dev/ttyUSB0'),
                'channel': c.get('bridge', 'channel', fallback='RNS'),
                'key': c.get('bridge', 'key', fallback='6b4296077a8f91ce4b1ee51b9962e8c3'),
                'name': c.get('bridge', 'name', fallback='RNS MeshCore'),
                'announce': c.getint('bridge', 'announce', fallback=10),
                'auth_password': c.get('bridge', 'auth_password', fallback=None),
                'news': news_val == 'on' or news_val == 'true' or news_val == '1',
                'news_file': c.get('bridge', 'news_file', fallback=''),
                'max_news': c.getint('bridge', 'max_news', fallback=50),
                'news_hash': c.get('bridge', 'news_hash', fallback='{self.news_hash}'),
                'baudrate': c.getint('bridge', 'baudrate', fallback=115200),
                'beacon_enabled': c.get('bridge', 'beacon_enabled', fallback='off').lower() in ('on', 'true', '1'),
                'beacon_interval': c.getint('bridge', 'beacon_interval', fallback=3600),
                'beacon_text': c.get('bridge', 'beacon_text', fallback='📡 Мост активен.'),
                'log_enabled': c.get('bridge', 'log_enabled', fallback='off').lower() in ('on', 'true', '1'),
                'log_retention_days': c.getint('bridge', 'log_retention_days', fallback=7),
            }
    return None


class Bridge:
    def __init__(self, cfg):
        print("=" * 50)
        self.running = True
        self.msg_count = 0
        print("RNS <-> MESHCORE BRIDGE")
        print("=" * 50)
        self.msg_count = 0

        self.storage = os.path.expanduser("~/.meshcore_bridge_storage")
        os.makedirs(self.storage, exist_ok=True)
        self.id_file = os.path.join(self.storage, 'bridge_identity')

        self.auth_password = cfg.get('auth_password')
        self.authorized_sessions = set()
        self.user_sessions = {}
        self.sessions = {}

        # Ники
        self.nicks = {}
        self.nicks_file = os.path.join(self.storage, "nicks.json")
        self._load_nicks()

        # Настройки новостей
        self.news_enabled = cfg.get('news', False)
        self.news_path = cfg.get('news_file', './news.mu')
        self.max_news = cfg.get('max_news', 50)
        # Маяк
        self.beacon_enabled = cfg.get('beacon_enabled', False)
        # Логирование
        self.log_enabled = cfg.get('log_enabled', False)
        self.log_retention_days = cfg.get('log_retention_days', 7)
        self.log_dir = os.path.join(self.storage, 'logs')
        if self.log_enabled:
            os.makedirs(self.log_dir, exist_ok=True)
            self._rotate_logs()
        self.beacon_interval = cfg.get('beacon_interval', 3600)
        self.beacon_text = cfg.get('beacon_text', '📡 Мост активен.')
        if self.beacon_enabled:
            print(f"[BEACON] Включён, интервал: {self.beacon_interval} сек")
            threading.Thread(target=self.beacon_loop, daemon=True).start()
        self.news_hash = cfg.get('news_hash', '{self.news_hash}')

        print(f"[NEWS] enabled={self.news_enabled}")
        print(f"[NEWS] path={self.news_path}")

        if self.news_enabled and os.path.exists(self.news_path):
            print(f"[NEWS] ✅ Файл найден")
        elif self.news_enabled:
            print(f"[NEWS] ⚠️ Файл НЕ НАЙДЕН: {self.news_path}")

        print(f"[AUTH] {'Включена' if self.auth_password else 'Выключена'}")

        # RNS
        self.rns = RNS.Reticulum(loglevel=RNS.LOG_CRITICAL)
        if os.path.exists(self.id_file):
            self.id = RNS.Identity.from_file(self.id_file)
        else:
            self.id = RNS.Identity()
            self.id.to_file(self.id_file)
        print(f"[RNS] Мост: {RNS.prettyhexrep(self.id.hash)[:16]}")

        self.router = LXMF.LXMRouter(identity=self.id, storagepath=self.storage)
        self.dest = self.router.register_delivery_identity(self.id, display_name=cfg['name'])
        self.router.register_delivery_callback(self.on_message)

        if cfg['announce'] > 0:
            try:
                self.router.announce(destination_hash=self.dest.hash)
                print("[ANNOUNCE] OK")
            except:
                pass

        # MeshCore
        self.port = cfg['port']
        self.baudrate = cfg.get('baudrate', 115200)
        self.channel_name = cfg['channel']
        self.channel_num = 1

        try:
            r = subprocess.run(f'meshcore-cli -s {self.port} get_channels', shell=True, timeout=10, capture_output=True,
                               text=True)
            for line in r.stdout.split('\n'):
                if self.channel_name.lower() in line.lower():
                    m = re.search(r'(\d+):', line)
                    if m:
                        self.channel_num = int(m.group(1))
                        break
            print(f"[MESH] Port: {self.port}, Channel: {self.channel_name} (ch{self.channel_num})")
        except:
            print(f"[MESH] Port: {self.port}, Channel: {self.channel_name} (ch{self.channel_num})")

        print("BRIDGE READY")
        self.msg_count = 0
        self.meshcore = None
        self.loop = None

        self.meshcore_thread = threading.Thread(target=self.run_meshcore, daemon=True)
        self.meshcore_thread.start()

    def _load_nicks(self):
        try:
            if os.path.exists(self.nicks_file):
                with open(self.nicks_file, 'r') as f:
                    self.nicks = json.load(f)
        except:
            self.nicks = {}

    def _save_nicks(self):
        try:
            with open(self.nicks_file, 'w') as f:
                json.dump(self.nicks, f)
        except:
            pass

    def run_meshcore(self):
        asyncio.run(self.meshcore_main())

    async def meshcore_main(self):
        try:
            connection = SerialConnection(self.port, self.baudrate)
            self.meshcore = MeshCore(connection)

            print(f"[MESH] Подключение к {self.port}...")
            await self.meshcore.connect()
            print("[MESH] Подключено!")

            self.loop = asyncio.get_event_loop()

            self.meshcore.subscribe(EventType.CHANNEL_MSG_RECV, self.on_channel_message)

            if hasattr(self.meshcore, 'start_auto_message_fetching'):
                await self.meshcore.start_auto_message_fetching()
                print("[MESH] Автополучение запущено")

            print(f"[MESH] Ожидание сообщений в канале {self.channel_num}...")

            while self.running:
                await asyncio.sleep(1)

        except Exception as e:
            print(f"[MESH] Ошибка: {e}")
            import traceback
            traceback.print_exc()

    def add_news_to_mu(self, nickname, text, source, channel_name=None):
        print(f"[NEWS] Добавление от @{nickname}: {text[:50]}...")

        if not self.news_enabled:
            print(f"[NEWS] ❌ Новости отключены в конфиге")
            return False

        if not os.path.exists(self.news_path):
            print(f"[NEWS] ❌ Файл не найден: {self.news_path}")
            return False

        now = datetime.datetime.now()
        date_str = now.strftime("%Y-%m-%d %H:%M:%S")

        safe_nick = nickname.replace('`', "'").replace('│', '|')
        safe_text = text.replace('`', "'").replace('│', '|')[:1000]

        if channel_name:
            source_display = f"`Fb3b` MeshCore`Ffff`b`f группа`f`Ff00 {channel_name}`f"
        else:
            source_display = f"`Fb3b` {source}`f"

        news_block = f"""│ 
│ `F6be` 📅 {date_str}`f
│ `F60f` 👤 @{safe_nick}`f `Ffff `b`f через`f {source_display}
│ `Ffff➥  `b`f {safe_text}`f
│ """

        try:
            with open(self.news_path, 'r', encoding='utf-8') as f:
                content = f.read()

            search_pattern = r'(`B48b`Ffff\n📌  Последние сообщения из эфира\n`b`f)'
            match = re.search(search_pattern, content)

            if match:
                new_content = content.replace(match.group(1), match.group(1) + '\n' + news_block)
                with open(self.news_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                print(f"[NEWS] ✅ Новость добавлена")
                return True
            else:
                print(f"[NEWS] ⚠️ Заголовок не найден в файле")
                return False

        except Exception as e:
            print(f"[NEWS] ❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()
            return False

    def on_channel_message(self, event):
        try:
            if not hasattr(event, 'payload'):
                return

            payload = event.payload
            if not isinstance(payload, dict):
                return

            channel = payload.get('channel_idx')
            if channel != self.channel_num:
                return

            text = payload.get('text', '')
            if not text or len(text) < 2:
                return

            if ': ' in text:
                parts = text.split(': ', 1)
                sender = parts[0].strip()
                msg_text = parts[1].strip()
            else:
                sender = "UNKNOWN"
                msg_text = text

            print(f"\n{GREEN}[RX]{RESET} {YELLOW}{sender}: {msg_text}{RESET}")
            self._log("from_meshcore", sender, None, msg_text, self.channel_name, None, None)

            if msg_text.lower().startswith('/news '):
                print(f"[NEWS] Обнаружена команда /news из MeshCore")
                news_content = msg_text[6:].strip()
                if news_content:
                    if self.add_news_to_mu(sender, news_content, self.channel_name, self.channel_name):
                        confirm = f"✅ Новость опубликована из MeshCore\n📡 Смотреть: [{self.news_hash}:/page/news.mu]"
                        self.send_to_meshcore(confirm)
                        self.broadcast_to_reticulum(confirm)
                return

            self.broadcast_to_reticulum(f"{sender}: {msg_text}")

        except Exception as e:
            print(f"[ERR] on_channel_message: {e}")

    def send_to_meshcore(self, text):
        if not self.meshcore or not self.loop:
            return False

        try:
            if hasattr(self.meshcore, 'commands') and hasattr(self.meshcore.commands, 'send_chan_msg'):
                async def send():
                    return await self.meshcore.commands.send_chan_msg(self.channel_num, text)

                future = asyncio.run_coroutine_threadsafe(send(), self.loop)
                future.result(timeout=5)
                return True
            return False
        except Exception as e:
            print(f"[ERR] send_to_meshcore: {e}")
            return False

    def send_to_reticulum(self, text, target_hash):
        try:
            recipient = RNS.Identity.recall(target_hash)
            if recipient:
                dest = RNS.Destination(recipient, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
                msg = LXMF.LXMessage(destination=dest, source=self.dest, content=text.encode('utf-8'))
                self.router.handle_outbound(msg)
                for _ in range(10):
                    self.router.process_outbound()
                    time.sleep(0.1)
                return True
        except Exception as e:
            print(f"[ERR] send_to_reticulum: {e}")
        return False

    def broadcast_to_reticulum(self, text, exclude_hash=None):
        if not self.sessions:
            return
        for hex_hash, data in self.sessions.items():
            if exclude_hash and hex_hash == exclude_hash:
                continue
            if self.auth_password and hex_hash not in self.authorized_sessions:
                continue
            self.send_to_reticulum(text, data['hash'])
            time.sleep(0.05)

    def on_message(self, message):
        try:
            text = message.content.decode() if isinstance(message.content, bytes) else str(message.content)
            source_hash = message.source_hash.hex() if isinstance(message.source_hash, bytes) else message.source_hash
            short_prefix = source_hash[:4].upper()

            self.sessions[source_hash] = {
                'hash': message.source_hash,
                'hex': source_hash,
                'name': short_prefix,
                'time': time.time()
            }

            print(f"{CYAN}[INFO]{RESET} Сессия: {YELLOW}{short_prefix}{RESET}")

            # Авторизация
            if text.strip().lower().startswith('/pass ') and self.auth_password:
                password = text.strip()[6:].strip()
                if password == self.auth_password:
                    self.authorized_sessions.add(source_hash)
                    self.send_to_reticulum("✅ Авторизация успешна! Этот мост управляет устройством LoRa в сети MeshCore.\n"
                    "Команды:  /𝙨𝙩𝙤𝙥 завершение сессии,  /𝙣𝙚𝙬𝙨 text для публикации сообщения на странице RNS, /𝙣𝙖𝙢𝙚 text для установки ника.", message.source_hash)
                else:
                    self.send_to_reticulum("⛔️ Неверный пароль!", message.source_hash)
                return

            if text.strip().lower() == '/stop':
                if source_hash in self.authorized_sessions:
                    self.authorized_sessions.discard(source_hash)
                    if source_hash in self.sessions:
                        del self.sessions[source_hash]
                    if source_hash in self.user_sessions:
                        del self.user_sessions[source_hash]
                    self.send_to_reticulum("❎ Сессия завершена.", message.source_hash)
                return

            # Команда /name
            if text.strip().lower().startswith('/name '):
                new_nick = text.strip()[6:].strip()
                if new_nick and len(new_nick) <= 20:
                    self.nicks[source_hash] = new_nick
                    self._save_nicks()
                    self.send_to_reticulum(f"✅ Ник установлен: {new_nick}", message.source_hash)
                else:
                    self.send_to_reticulum("❌ Ник должен быть от 1 до 20 символов", message.source_hash)
                return

            if not self.auth_password and source_hash not in self.user_sessions:
                self.user_sessions[source_hash] = True
                self.authorized_sessions.add(source_hash)
                self.send_to_reticulum(
                    "✅ Сессия активирована. Этот мост управляет устройством LoRa на базе Heltec V3 в сети MeshCore г.Москва.\n"
                    "Команды:  /𝙨𝙩𝙤𝙥 завершение сессии,  /𝙣𝙚𝙬𝙨 text для публикации сообщения на странице RNS, /𝙣𝙖𝙢𝙚 text для установки ника.",
                    message.source_hash
                )
                return

            if self.auth_password and source_hash not in self.authorized_sessions:
                self.send_to_reticulum(
                    "⚠️ Требуется авторизация! /𝙥𝙖𝙨𝙨 𝙋𝘼𝙎𝙎𝙒𝙊𝙍𝘿",
                    message.source_hash
                )
                return

            # Обработка /news из Reticulum
            if text.strip().lower().startswith('/news '):
                print(f"[NEWS] Обнаружена команда /news из Reticulum от {short_prefix}")
                news_content = text[6:].strip()
                if news_content:
                    if self.add_news_to_mu(short_prefix, news_content, "MeshChat"):
                        confirm = f"✅ Новость опубликована из MeshChat\n📡 Смотреть: [{self.news_hash}:/page/news.mu]"
                        self.send_to_reticulum(confirm, message.source_hash)
                        self.send_to_meshcore(confirm)
                return

            self.msg_count += 1
            display_name = self.nicks.get(source_hash, short_prefix)
            self._log("from_reticulum", display_name, source_hash, text, self.channel_name, None, None)
            print(f"\n{CYAN}[TX]{RESET} @{BLUE}{display_name}{RESET}: {text}")

            mesh_msg = f"{display_name}: {text}"
            self.send_to_meshcore(mesh_msg)
            self.broadcast_to_reticulum(f"{display_name}: {text}", exclude_hash=source_hash)

        except Exception as e:
            print(f"[ERR] on_message: {e}")

    def beacon_loop(self):
        while self.running:
            time.sleep(self.beacon_interval)
            if self.beacon_enabled:
                self.send_to_meshcore(self.beacon_text)
                print(f"[BEACON] Отправлен маяк: {self.beacon_text[:50]}...")
    def run(self):
        last = time.time()
        while self.running:
            try:
                self.router.process_outbound()
            except:
                pass
            if time.time() - last >= 600:
                try:
                    self.router.announce(destination_hash=self.dest.hash)
                    last = time.time()
                except:
                    pass
            time.sleep(0.3)

    def _rotate_logs(self):
        """Ротация и сжатие старых логов"""
        if not self.log_enabled:
            return
        try:
            import glob, gzip, shutil
            now = datetime.datetime.now()
            for f in glob.glob(os.path.join(self.log_dir, 'bridge_*.log')):
                try:
                    date_str = f.split('_')[-1].split('.')[0]
                    log_date = datetime.datetime.strptime(date_str, '%Y%m%d')
                    if (now - log_date).days > self.log_retention_days:
                        os.remove(f)
                        print(f"[LOG] Удалён старый лог: {f}")
                except:
                    pass
            yesterday = (now - datetime.timedelta(days=1)).strftime('%Y%m%d')
            log_file = os.path.join(self.log_dir, f'bridge_{yesterday}.log')
            if os.path.exists(log_file):
                gz_file = log_file + '.gz'
                if not os.path.exists(gz_file):
                    with open(log_file, 'rb') as f_in:
                        with gzip.open(gz_file, 'wb') as f_out:
                            shutil.copyfileobj(f_in, f_out)
                    os.remove(log_file)
                    print(f"[LOG] Сжат лог за {yesterday}")
        except Exception as e:
            print(f"[LOG] Ошибка ротации: {e}")

    def _log(self, direction, sender, sender_hash, text, channel_name=None, channel_hash=None, recipient=None):
        if not self.log_enabled:
            return
        try:
            import json, hashlib
            today = datetime.datetime.now().strftime('%Y%m%d')
            log_file = os.path.join(self.log_dir, f'bridge_{today}.log')
            entry = {
                "ts": datetime.datetime.now().isoformat(),
                "dir": direction,
                "from": sender,
                "from_hash": sender_hash[:16] if sender_hash else None,
                "text": text[:500],
                "text_hash": hashlib.sha256(text.encode('utf-8')).hexdigest()[:16],
                "channel": channel_name,
                "channel_hash": channel_hash[:16] if channel_hash else None,
                "to": recipient,
                "ver": "v30.8.8"
            }
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except Exception as e:
            print(f"[LOG] Ошибка: {e}")

if __name__ == "__main__":
    cfg = load_config()
    if not cfg:
        print("ОШИБКА: Не найден bridge_config.ini")
        sys.exit(1)

    bridge = Bridge(cfg)
    try:
        bridge.run()
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        bridge.running = False

    def _rotate_logs(self):
        """Ротация и сжатие старых логов"""
        if not self.log_enabled:
            return
        try:
            import glob, gzip, shutil
            now = datetime.datetime.now()
            # Удаляем логи старше retention_days
            for f in glob.glob(os.path.join(self.log_dir, 'bridge_*.log')):
                try:
                    date_str = f.split('_')[-1].split('.')[0]
                    log_date = datetime.datetime.strptime(date_str, '%Y%m%d')
                    if (now - log_date).days > self.log_retention_days:
                        os.remove(f)
                        print(f"[LOG] Удалён старый лог: {f}")
                except:
                    pass
            # Сжимаем вчерашний лог, если ещё не сжат
            yesterday = (now - datetime.timedelta(days=1)).strftime('%Y%m%d')
            log_file = os.path.join(self.log_dir, f'bridge_{yesterday}.log')
            if os.path.exists(log_file):
                gz_file = log_file + '.gz'
                if not os.path.exists(gz_file):
                    with open(log_file, 'rb') as f_in:
                        with gzip.open(gz_file, 'wb') as f_out:
                            shutil.copyfileobj(f_in, f_out)
                    os.remove(log_file)
                    print(f"[LOG] Сжат лог за {yesterday}")
        except Exception as e:
            print(f"[LOG] Ошибка ротации: {e}")

    def _log(self, direction, sender, sender_hash, text, channel_name=None, channel_hash=None, recipient=None):
        if not self.log_enabled:
            return
        try:
            import json, hashlib
            today = datetime.datetime.now().strftime('%Y%m%d')
            log_file = os.path.join(self.log_dir, f'bridge_{today}.log')
            entry = {
                "ts": datetime.datetime.now().isoformat(),
                "dir": direction,
                "from": sender,
                "from_hash": sender_hash[:16] if sender_hash else None,
                "text": text[:500],
                "text_hash": hashlib.sha256(text.encode('utf-8')).hexdigest()[:16],
                "channel": channel_name,
                "channel_hash": channel_hash[:16] if channel_hash else None,
                "to": recipient,
                "ver": "v1.1.0"
            }
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except Exception as e:
            print(f"[LOG] Ошибка: {e}")
