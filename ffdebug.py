#!/usr/bin/env python3
"""Debug userChrome.css in a throwaway Firefox profile over Marionette.

    ./ffdebug.py launch            # start Firefox with the debug profile
    ./ffdebug.py reload            # reload chrome/userChrome.css
    ./ffdebug.py watch             # reload on save
    ./ffdebug.py shot ui.png       # capture the chrome window
    ./ffdebug.py inspect '#nav-bar' '#urlbar-container'
    ./ffdebug.py check             # report rules and declarations Firefox threw away
    ./ffdebug.py stop
"""
import argparse
import base64
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(REPO, "debugprofile")
SHEET = os.path.join(REPO, "chrome", "userChrome.css")
PIDFILE = os.path.join(PROFILE, "ffdebug.pid")

HOST = "127.0.0.1"
PORT = int(os.environ.get("FFDEBUG_PORT", "2828"))
BINARIES = ("firefox-developer-edition", "firefox", "firefox-nightly", "firefox-esr")

# Firefox 155 toolbar layout.
TOOLBAR_STATE = {
    "placements": {
        "widget-overflow-fixed-list": [],
        "unified-extensions-area": [],
        "nav-bar": [
            "back-button",
            "forward-button",
            "stop-reload-button",
            "vertical-spacer",
            "urlbar-container",
            "downloads-button",
            "developer-button",
            "fxa-toolbar-menu-button",
            "reset-pbm-toolbar-button",
            "unified-extensions-button",
        ],
        "toolbar-menubar": ["menubar-items"],
        "TabsToolbar": [
            "tabbrowser-tabs",
            "new-tab-button",
            "alltabs-button",
            "ai-window-toggle",
        ],
        "vertical-tabs": [],
        "PersonalToolbar": ["personal-bookmarks"],
    },
    # Prevent automatic widget placement.
    "seen": [
        "reset-pbm-toolbar-button",
        "developer-button",
        "profiler-button",
        "ai-window-toggle",
        "screenshot-button",
    ],
    "dirtyAreaCache": ["nav-bar", "TabsToolbar", "vertical-tabs"],
    # Match CustomizableUI.kVersion to avoid migrations.
    "currentVersion": 26,
    "newElementCount": 0,
}

PREFS = {
    "marionette.port": PORT,
    # Quiet startup: no onboarding, telemetry, updates, or session restore.
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "browser.aboutwelcome.enabled": False,
    "browser.startup.page": 0,
    "browser.startup.homepage": "about:blank",
    "browser.newtabpage.enabled": False,
    "browser.warnOnQuit": False,
    "browser.tabs.warnOnClose": False,
    "browser.sessionstore.resume_from_crash": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "datareporting.policy.firstRunURL": "",
    "datareporting.healthreport.uploadEnabled": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "app.update.auto": False,
    "app.update.checkInstallTime": False,
    "extensions.pocket.enabled": False,
    # Chrome debugging affordances.
    "devtools.chrome.enabled": True,
    "devtools.debugger.remote-enabled": True,
    "layout.css.report_errors": True,
}


# --- Marionette ------------------------------------------------------------

class Marionette:
    def __init__(self, port=PORT, timeout=30):
        try:
            self.sock = socket.create_connection((HOST, port), timeout=timeout)
        except OSError:
            sys.exit(f"no Marionette on {HOST}:{port} -- run './ffdebug.py launch' first")
        self.buf = b""
        self.msgid = 0
        try:
            self._read()  # Handshake.
            self.cmd("WebDriver:NewSession", {"capabilities": {}})
        except ConnectionError:
            # Marionette drops new connections while its single session is busy.
            sys.exit("Marionette is busy -- another client holds the session")
        self.cmd("Marionette:SetContext", {"value": "chrome"})

    def _recv(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("Marionette closed the connection")
        return chunk

    def _read(self):
        # Marionette frames messages as "<length>:<payload>".
        while b":" not in self.buf:
            self.buf += self._recv()
        n, _, rest = self.buf.partition(b":")
        n = int(n)
        while len(rest) < n:
            rest += self._recv()
        self.buf = rest[n:]
        return json.loads(rest[:n])

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def cmd(self, name, params=None):
        self.msgid += 1
        payload = json.dumps([0, self.msgid, name, params or {}]).encode()
        self.sock.sendall(b"%d:%s" % (len(payload), payload))
        msg = self._read()
        if isinstance(msg, list) and len(msg) == 4:
            if msg[2]:
                raise RuntimeError(msg[2].get("message") or json.dumps(msg[2], indent=2))
            return unwrap(msg[3])
        return unwrap(msg)

    def js(self, script, *args):
        return self.cmd("WebDriver:ExecuteScript", {"script": script, "args": list(args)})

    def element(self, selector):
        try:
            found = self.cmd(
                "WebDriver:FindElement", {"using": "css selector", "value": selector}
            )
        except RuntimeError:
            return None
        return next(iter(found.values())) if isinstance(found, dict) else found


def unwrap(result):
    if isinstance(result, dict) and set(result) == {"value"}:
        return result["value"]
    return result


# --- Process control -------------------------------------------------------

def firefox_binary():
    override = os.environ.get("FFDEBUG_FIREFOX")
    if override:
        return override
    for name in BINARIES:
        path = shutil.which(name)
        if path:
            return path
    sys.exit("no Firefox binary found -- set FFDEBUG_FIREFOX=/path/to/firefox")


def write_profile(fresh=False):
    if fresh and os.path.isdir(PROFILE):
        shutil.rmtree(PROFILE)
    os.makedirs(PROFILE, exist_ok=True)
    lines = ["// Generated by ffdebug.py -- rewritten on every launch.\n"]
    for name, value in PREFS.items():
        lines.append(f"user_pref({json.dumps(name)}, {json.dumps(value)});\n")
    lines.append("\n// Fresh-profile toolbar without the address bar's flexible springs.\n")
    lines.append(
        "user_pref(\"browser.uiCustomization.state\", "
        f"{json.dumps(json.dumps(TOOLBAR_STATE, separators=(',', ':')))});\n"
    )
    with open(os.path.join(PROFILE, "user.js"), "w") as f:
        f.writelines(lines)


def running_pid():
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def port_open(port=PORT):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, port)) == 0


def wait_for_port(port=PORT, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port):
            return True
        time.sleep(0.2)
    return False


def do_launch(args):
    if running_pid() or port_open():
        sys.exit("already running -- use './ffdebug.py restart' or './ffdebug.py stop'")
    write_profile(fresh=args.fresh)

    env = dict(os.environ)
    if args.headless:
        env["MOZ_HEADLESS"] = "1"
    argv = [
        firefox_binary(),
        "--no-remote",
        "--profile", PROFILE,
        "--marionette",
        # Required for chrome-context evaluation since Firefox 129.
        "--remote-allow-system-access",
    ]
    if args.url:
        argv.append(args.url)

    proc = subprocess.Popen(argv, env=env)
    with open(PIDFILE, "w") as f:
        f.write(str(proc.pid))

    def interrupt(_signum, _frame):
        # Route termination signals through child cleanup.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    try:
        if not wait_for_port():
            sys.exit(f"Marionette never came up on port {PORT}")

        # Release the single Marionette session before waiting.
        with Marionette() as m:
            if args.width and args.height:
                resize(m, args.width, args.height)
            sheet = register_sheet(m, args.css)
        print(f"pid {proc.pid}  profile {PROFILE}  marionette {PORT}", file=sys.stderr)
        print(sheet, file=sys.stderr)
        print("running -- ctrl-c to quit", file=sys.stderr)
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait()
        if os.path.exists(PIDFILE):
            os.remove(PIDFILE)


def do_stop(args):
    pid = running_pid()
    if not pid:
        if os.path.exists(PIDFILE):
            os.remove(PIDFILE)
        print("not running")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    if os.path.exists(PIDFILE):
        os.remove(PIDFILE)
    print(f"stopped {pid}")


def do_restart(args):
    do_stop(args)
    # Wait for Marionette to release the port.
    for _ in range(50):
        if not port_open():
            break
        time.sleep(0.1)
    do_launch(args)


def do_status(args):
    pid = running_pid()
    print(f"pid        {pid or '-'}")
    print(f"marionette {PORT} {'up' if port_open() else 'down'}")
    print(f"profile    {PROFILE}")
    if not (pid and port_open()):
        return
    m = Marionette()
    info = m.js("""
        return {
          version: Services.appinfo.version,
          sheet: Services.prefs.getStringPref("ffdebug.sheet", ""),
          windows: Services.wm.getEnumerator("navigator:browser").length ?? undefined,
        };
    """)
    print(f"firefox    {info['version']}")
    print(f"sheet      {info['sheet'] or '-'}")


# --- Stylesheet ------------------------------------------------------------

REGISTER_JS = """
    const [path] = arguments;
    const sss = Cc["@mozilla.org/content/style-sheet-service;1"]
                  .getService(Ci.nsIStyleSheetService);
    const drop = spec => {
      const uri = Services.io.newURI(spec);
      if (sss.sheetRegistered(uri, sss.USER_SHEET)) {
        sss.unregisterSheet(uri, sss.USER_SHEET);
      }
    };
    // Remove the previous path if the sheet moved.
    const previous = Services.prefs.getStringPref("ffdebug.sheet", "");
    if (previous) { drop(previous); }
    const spec = PathUtils.toFileURI(path);
    drop(spec);
    sss.loadAndRegisterSheet(Services.io.newURI(spec), sss.USER_SHEET);
    Services.prefs.setStringPref("ffdebug.sheet", spec);
    return spec;
"""


def register_sheet(m, css):
    path = os.path.abspath(css)
    if not os.path.isfile(path):
        sys.exit(f"no such stylesheet: {path}")
    m.js(REGISTER_JS, path)
    return f"loaded {os.path.relpath(path, REPO)}"


def do_reload(args):
    with Marionette() as m:
        print(register_sheet(m, args.css))


def do_watch(args):
    path = os.path.abspath(args.css)
    sys.stdout.reconfigure(line_buffering=True)
    do_reload(args)
    print("watching -- ctrl-c to stop")
    last = os.stat(path).st_mtime_ns
    while True:
        time.sleep(0.3)
        try:
            now = os.stat(path).st_mtime_ns
        except FileNotFoundError:
            continue  # Mid-save, for editors that write via rename.
        if now == last:
            continue
        last = now
        # Release Marionette between reloads.
        with Marionette() as m:
            print(f"{time.strftime('%H:%M:%S')} {register_sheet(m, path)}")


# --- Inspection ------------------------------------------------------------

INSPECT_JS = """
    const [selectors, props] = arguments;
    return selectors.map(sel => {
      const el = document.querySelector(sel);
      if (!el) { return {selector: sel, found: false}; }
      const r = el.getBoundingClientRect();
      const cs = window.getComputedStyle(el);
      const style = {};
      for (const p of props) { style[p] = cs.getPropertyValue(p); }
      return {
        selector: sel,
        found: true,
        id: el.id || null,
        rect: {
          x: Math.round(r.x), y: Math.round(r.y),
          width: Math.round(r.width), height: Math.round(r.height),
        },
        hidden: r.width === 0 && r.height === 0,
        style,
      };
    });
"""

DEFAULT_PROPS = ("display", "position", "order", "flex", "width", "height")


def do_inspect(args):
    props = args.props.split(",") if args.props else list(DEFAULT_PROPS)
    results = Marionette().js(INSPECT_JS, args.selectors, props)
    if args.json:
        print(json.dumps(results, indent=2))
        return
    for r in results:
        if not r["found"]:
            print(f"{r['selector']}: not found")
            continue
        rect = r["rect"]
        flag = "  (zero-size)" if r["hidden"] else ""
        print(f"{r['selector']}{flag}")
        print(f"  rect  x={rect['x']} y={rect['y']} w={rect['width']} h={rect['height']}")
        for name, value in r["style"].items():
            print(f"  {name:<12} {value}")


def do_eval(args):
    script = sys.stdin.read() if args.script == "-" else args.script
    m = Marionette()
    # Try an expression first, then retry syntax errors as a statement body.
    try:
        result = m.js("return (\n" + script + "\n);")
    except RuntimeError as e:
        if "SyntaxError" not in str(e):
            raise
        result = m.js(script)
    print(json.dumps(result, indent=2))


def do_shot(args):
    m = Marionette()
    params = {"full": True, "hash": False}
    if args.selector:
        ref = m.element(args.selector)
        if not ref:
            sys.exit(f"no element matches {args.selector}")
        params = {"id": ref, "full": False, "hash": False}
    data = m.cmd("WebDriver:TakeScreenshot", params)
    with open(args.out, "wb") as f:
        f.write(base64.b64decode(data))
    print(args.out)


ERRORS_JS = """
    const [clear] = arguments;
    const out = [];
    for (const msg of Services.console.getMessageArray() || []) {
      try {
        const e = msg.QueryInterface(Ci.nsIScriptError);
        out.push({
          kind: e.isWarning ? "warning" : "error",
          category: e.category,
          message: e.errorMessage,
          source: e.sourceName,
          line: e.lineNumber,
        });
      } catch (_) {
        out.push({kind: "log", message: String(msg.message ?? msg)});
      }
    }
    // Console API messages use separate storage.
    const storage = Cc["@mozilla.org/consoleAPI-storage;1"]
                      .getService(Ci.nsIConsoleAPIStorage);
    for (const e of storage.getEvents(null) || []) {
      if (e.level !== "error" && e.level !== "warn") { continue; }
      out.push({
        kind: e.level === "warn" ? "warning" : "error",
        category: "console",
        message: (e.arguments || []).map(String).join(" "),
        source: e.filename,
        line: e.lineNumber,
      });
    }
    if (clear) { Services.console.reset(); storage.clearEvents(); }
    return out;
"""


def do_errors(args):
    with Marionette() as m:
        entries = m.js(ERRORS_JS, args.clear)
    if not entries:
        print("no errors")
        return
    for e in entries:
        where = e.get("source") or ""
        if where and e.get("line"):
            where += f":{e['line']}"
        head = " ".join(p for p in (e.get("category"), where) if p)
        print(f"[{e['kind']}] {head}\n  {e['message']}")


TOOLBAR_JS = """
    const CUI = window.CustomizableUI;
    const areas = {};
    for (const area of CUI.areas) {
      try { areas[area] = CUI.getWidgetIdsInArea(area); } catch (e) { areas[area] = null; }
    }
    return areas;
"""


def do_toolbar(args):
    areas = Marionette().js(TOOLBAR_JS)
    if args.json:
        print(json.dumps(areas, indent=2))
        return
    expected = TOOLBAR_STATE["placements"]
    for area, widgets in areas.items():
        drift = "" if widgets == expected.get(area) else "  (differs from baseline)"
        print(f"{area}{drift}")
        for w in widgets or []:
            print(f"  {w}")


def do_pref(args):
    if args.value is None:
        value = Marionette().js("""
            const [name] = arguments;
            switch (Services.prefs.getPrefType(name)) {
              case Services.prefs.PREF_BOOL:   return Services.prefs.getBoolPref(name);
              case Services.prefs.PREF_INT:    return Services.prefs.getIntPref(name);
              case Services.prefs.PREF_STRING: return Services.prefs.getStringPref(name);
              default: return null;
            }
        """, args.name)
        print(json.dumps(value))
        return
    # Parse JSON values; otherwise use a string.
    try:
        parsed = json.loads(args.value)
    except json.JSONDecodeError:
        parsed = args.value
    Marionette().js("""
        const [name, value] = arguments;
        if (typeof value === "boolean") { Services.prefs.setBoolPref(name, value); }
        else if (Number.isInteger(value)) { Services.prefs.setIntPref(name, value); }
        else { Services.prefs.setStringPref(name, String(value)); }
    """, args.name, parsed)
    print(f"{args.name} = {json.dumps(parsed)}")


def do_open(args):
    Marionette().js("""
        const [url] = arguments;
        Services.wm.getMostRecentWindow("navigator:browser")
                .openTrustedLinkIn(url, "tab");
    """, args.url)
    print(args.url)


def do_window(args):
    Marionette().js("""
        const [priv] = arguments;
        Services.wm.getMostRecentWindow("navigator:browser")
                .OpenBrowserWindow({private: priv});
    """, args.private)
    print("private window" if args.private else "window")


def resize(m, width, height):
    m.js("window.resizeTo(arguments[0], arguments[1]);", width, height)


def do_resize(args):
    m = Marionette()
    resize(m, args.width, args.height)
    size = m.js("return {width: window.outerWidth, height: window.outerHeight};")
    print(f"{size['width']}x{size['height']}")


# --- CSS check -------------------------------------------------------------

def split_css(text, base=0):
    """Return top-level chunks as (source offset, text) pairs."""
    chunks, depth, start, i, n = [], 0, 0, 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and text[i + 1:i + 2] == "*":
            end = text.find("*/", i + 2)
            after = n if end < 0 else end + 2
            if depth == 0 and not text[start:i].strip():
                start = after  # Exclude leading comments from the rule offset.
            i = after
            continue
        if c in "\"'":
            i += 1
            while i < n and text[i] != c:
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
            if depth == 0:
                chunks.append((start, text[start:i + 1]))
                start = i + 1
        elif c == ";" and depth == 0:
            chunks.append((start, text[start:i + 1]))
            start = i + 1
        i += 1
    if text[start:].strip():
        chunks.append((start, text[start:]))
    return [
        (base + idx + len(c) - len(c.lstrip()), c.strip())
        for idx, c in chunks
        if c.strip()
    ]


DECL_RE = re.compile(r"^\s*(--[-\w]+|[-\w]+)\s*:\s*(.+?)\s*(?:!important)?\s*$", re.S)


def walk_rules(text, base=0):
    """Yield source offsets and declarations, including nested rules."""
    for idx, chunk in split_css(text, base):
        brace = chunk.find("{")
        if brace < 0 or not chunk.endswith("}"):
            continue  # Ignore bare at-statements such as @import.
        body_at = idx + brace + 1
        body = chunk[brace + 1:-1]
        if chunk.startswith("@"):
            yield idx, []
            yield from walk_rules(body, body_at)
        else:
            props = []
            for decl_idx, decl in split_css(body, body_at):
                m = DECL_RE.match(decl.rstrip(";"))
                if m:
                    props.append((decl_idx, m.group(1), m.group(2)))
            yield idx, props


# Scratch <style> elements reject chrome-only properties, so inspect the user sheet.
CHECK_JS = """
    const [href, sources] = arguments;
    const sheet = InspectorUtils.getAllStyleSheets(document, false)
                                .find(s => s.href === href);
    if (!sheet) { return null; }
    const byLine = new Map();
    const walk = rules => {
      for (const rule of rules) {
        const line = InspectorUtils.getRelativeRuleLine(rule);
        if (!byLine.has(line)) { byLine.set(line, []); }
        byLine.get(line).push(rule);
        if (rule.cssRules) { walk(rule.cssRules); }
      }
    };
    walk(sheet.cssRules);
    return sources.map(([line, props]) => {
      const rule = (byLine.get(line) || []).shift();
      if (!rule) { return {found: false, dropped: []}; }
      // Empty properties were rejected by the parser.
      const dropped = rule.style
        ? props.filter(p => !rule.style.getPropertyValue(p))
        : [];
      return {found: true, dropped};
    });
"""


def do_check(args):
    path = os.path.abspath(args.css)
    with open(path) as f:
        text = f.read()
    rules = list(walk_rules(text))

    def line_of(idx):
        return text.count("\n", 0, idx) + 1

    m = Marionette()
    # Refresh the live sheet from disk before checking it.
    href = m.js(REGISTER_JS, path)
    results = m.js(
        CHECK_JS, href, [[line_of(idx), [p for _, p, _ in props]] for idx, props in rules]
    )
    if results is None:
        sys.exit(f"{path} is not registered in the browser")

    problems = 0
    for (idx, props), result in zip(rules, results):
        if not result["found"]:
            problems += 1
            head = text[idx:idx + 120].split("{")[0].strip().replace("\n", " ")
            print(f"{path}:{line_of(idx)}: rule dropped -- {head[:90]}")
            continue
        for decl_idx, prop, value in props:
            if prop in result["dropped"]:
                problems += 1
                print(
                    f"{path}:{line_of(decl_idx)}: declaration dropped -- "
                    f"{prop}: {value[:70]}"
                )

    decls = sum(len(p) for _, p in rules)
    print(f"{len(rules)} rules, {decls} declarations, {problems} problem(s)")
    return 1 if problems else 0


# --- CLI -------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    def launch_args(sp):
        sp.add_argument("--fresh", action="store_true", help="wipe the profile first")
        sp.add_argument("--headless", action="store_true")
        sp.add_argument("--url", help="page to open on startup")
        sp.add_argument("--width", type=int)
        sp.add_argument("--height", type=int)
        sp.add_argument("--css", default=SHEET)

    sp = sub.add_parser("launch", help="start Firefox on the debug profile")
    launch_args(sp)
    sp.set_defaults(func=do_launch)

    sp = sub.add_parser("restart", help="stop, then launch again")
    launch_args(sp)
    sp.set_defaults(func=do_restart)

    sub.add_parser("stop", help="quit the debug browser").set_defaults(func=do_stop)
    sub.add_parser("status", help="show what is running").set_defaults(func=do_status)

    sp = sub.add_parser("reload", help="re-apply the stylesheet")
    sp.add_argument("css", nargs="?", default=SHEET)
    sp.set_defaults(func=do_reload)

    sp = sub.add_parser("watch", help="reload whenever the stylesheet changes")
    sp.add_argument("css", nargs="?", default=SHEET)
    sp.set_defaults(func=do_watch)

    sp = sub.add_parser("check", help="report rules and declarations Firefox rejects")
    sp.add_argument("css", nargs="?", default=SHEET)
    sp.set_defaults(func=do_check)

    sp = sub.add_parser("shot", help="screenshot the chrome window")
    sp.add_argument("out")
    sp.add_argument("--selector", help="crop to the first matching element")
    sp.set_defaults(func=do_shot)

    sp = sub.add_parser("inspect", help="rects and computed styles for selectors")
    sp.add_argument("selectors", nargs="+")
    sp.add_argument("--props", help="comma-separated property list")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=do_inspect)

    sp = sub.add_parser("eval", help="run privileged JS ('-' reads stdin)")
    sp.add_argument("script")
    sp.set_defaults(func=do_eval)

    sp = sub.add_parser("errors", help="drain the browser error console")
    sp.add_argument("--clear", action="store_true")
    sp.set_defaults(func=do_errors)

    sp = sub.add_parser("toolbar", help="show widget placement per toolbar area")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=do_toolbar)

    sp = sub.add_parser("pref", help="read or write a pref")
    sp.add_argument("name")
    sp.add_argument("value", nargs="?")
    sp.set_defaults(func=do_pref)

    sp = sub.add_parser("open", help="open a URL in a new tab")
    sp.add_argument("url")
    sp.set_defaults(func=do_open)

    sp = sub.add_parser("window", help="open another browser window")
    sp.add_argument("--private", action="store_true")
    sp.set_defaults(func=do_window)

    sp = sub.add_parser("resize", help="resize the chrome window")
    sp.add_argument("width", type=int)
    sp.add_argument("height", type=int)
    sp.set_defaults(func=do_resize)

    args = p.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
