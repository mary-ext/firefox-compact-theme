#!/usr/bin/env python3
"""Run privileged JavaScript in Firefox's chrome window via Marionette.

  ./ffdebug.py eval  '<js>'     -> JSON result of the script
  ./ffdebug.py shot  <out.png>  -> screenshot of the browser chrome
  ./ffdebug.py reload <css>     -> reload a user stylesheet
"""
import base64, json, socket, sys

HOST, PORT = "127.0.0.1", 2828


class Marionette:
    def __init__(self):
        self.sock = socket.create_connection((HOST, PORT), timeout=30)
        self.buf = b""
        self.msgid = 0
        self._read()  # Handshake.

    def _read(self):
        # Marionette frames messages as "<length>:<payload>".
        while b":" not in self.buf:
            self.buf += self.sock.recv(65536)
        n, _, rest = self.buf.partition(b":")
        n = int(n)
        while len(rest) < n:
            rest += self.sock.recv(65536)
        self.buf = rest[n:]
        return json.loads(rest[:n])

    def cmd(self, name, params=None):
        self.msgid += 1
        payload = json.dumps([0, self.msgid, name, params or {}]).encode()
        self.sock.sendall(b"%d:%s" % (len(payload), payload))
        msg = self._read()
        if isinstance(msg, list) and len(msg) == 4:
            if msg[2]:
                raise RuntimeError(json.dumps(msg[2], indent=2))
            return msg[3]
        return msg


def main():
    m = Marionette()
    m.cmd("WebDriver:NewSession", {"capabilities": {}})
    m.cmd("Marionette:SetContext", {"value": "chrome"})
    mode = sys.argv[1]

    if mode == "eval":
        r = m.cmd("WebDriver:ExecuteScript", {"script": sys.argv[2], "args": []})
        print(json.dumps(r.get("value") if isinstance(r, dict) else r, indent=2))
    elif mode == "shot":
        r = m.cmd("WebDriver:TakeScreenshot", {"full": True, "hash": False})
        data = r.get("value") if isinstance(r, dict) else r
        with open(sys.argv[2], "wb") as f:
            f.write(base64.b64decode(data))
        print(sys.argv[2])
    elif mode == "reload":
        m.cmd("WebDriver:ExecuteScript", {"script": """
            const {Services} = ChromeUtils.importESModule(
              'resource://gre/modules/Services.sys.mjs');
            Services.obs.notifyObservers(null, 'startupcache-invalidate');
            const u = Services.io.newURI('file://' + arguments[0]);
            const sss = Cc['@mozilla.org/content/style-sheet-service;1']
                          .getService(Ci.nsIStyleSheetService);
            if (sss.sheetRegistered(u, sss.USER_SHEET)) sss.unregisterSheet(u, sss.USER_SHEET);
            sss.loadAndRegisterSheet(u, sss.USER_SHEET);
            return 'ok';
        """, "args": [sys.argv[2]]})
        print("reloaded")


main()
