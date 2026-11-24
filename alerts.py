"""Optional, best-effort Telegram notifications. Never blocks the strategy loop."""
import atexit
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.request

class Telegram(logging.Handler):
    def __init__(self, token, chat):
        super().__init__()
        self.url='https://api.telegram.org/bot'+token+'/sendMessage'
        self.chat=chat
        self.messages=queue.Queue(maxsize=32)
        self.last={}
        self.worker=threading.Thread(target=self._work,daemon=True)
        self.worker.start()
        atexit.register(self.finish)
    def emit(self, record):
        if not getattr(record,'notify',False): return
        msg=record.getMessage()
        stamp=time.monotonic()
        if stamp-self.last.get(msg,-1e9)<300: return
        self.last[msg]=stamp
        try: self.messages.put_nowait(msg)
        except queue.Full: pass
    def _work(self):
        while True:
            msg=self.messages.get()
            try:
                if msg is None: return
                payload=json.dumps({'chat_id':self.chat,'text':msg[:4000]}).encode()
                req=urllib.request.Request(self.url,data=payload,headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=4) as r:
                    if not json.load(r).get('ok'): raise RuntimeError('Telegram rejected message')
            except Exception:
                # Do not print the request URL (it contains the bot token).
                logging.getLogger('expiry.alerts').warning('Telegram delivery failed; trading simulation continues')
            finally: self.messages.task_done()
    def finish(self):
        try: self.messages.put_nowait(None)
        except queue.Full: return
        self.worker.join(timeout=6)

def configure_alerts(logger):
    token=os.environ.get('TELEGRAM_BOT_TOKEN','').strip()
    chat=os.environ.get('TELEGRAM_CHAT_ID','').strip()
    if not token and not chat: return
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+',token) or not re.fullmatch(r'-?\d+',chat):
        logger.warning('Telegram disabled: invalid token or chat ID')
        return
    logger.addHandler(Telegram(token,chat))
