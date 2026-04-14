"""
SW Version Tracker - Local Server with SQLite
Run: python server.py  →  open http://localhost:5000
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import subprocess, json, os, sqlite3, urllib.parse, base64, re, time

PORT     = 5000
DB_FILE  = "tracker.db"
UPLOADS  = "uploads"

# ── Database ──────────────────────────────────────────────────────────────────

def db():
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    os.makedirs(UPLOADS, exist_ok=True)
    c = db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entries (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ver        TEXT NOT NULL,
            details    TEXT DEFAULT '',
            case_id    TEXT DEFAULT '',
            customer   TEXT DEFAULT '',
            issue      TEXT DEFAULT '',
            notes      TEXT DEFAULT '',
            images     TEXT DEFAULT '[]',
            path       TEXT DEFAULT '',
            paths      TEXT DEFAULT '[]',
            status     TEXT DEFAULT 'workable',
            category   TEXT DEFAULT 'AOI',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # add missing columns for existing databases
    existing = {r[1] for r in c.execute("PRAGMA table_info(entries)")}
    for col, defn in [("notes","TEXT DEFAULT ''"), ("images","TEXT DEFAULT '[]'"),
                      ("status","TEXT DEFAULT 'workable'"), ("category","TEXT DEFAULT 'AOI'"),
                      ("paths","TEXT DEFAULT '[]'"), ("path","TEXT DEFAULT ''")]:
        if col not in existing:
            c.execute(f"ALTER TABLE entries ADD COLUMN {col} {defn}")
            print(f"  [DB] added column '{col}'")

    # seed categories
    for name in ["AOI","SPI","MES"]:
        c.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (name,))

    # migrate single path → paths array
    rows = c.execute("SELECT id,path FROM entries WHERE paths='[]' AND path!=''").fetchall()
    for row in rows:
        label = row["path"].replace("\\","/").split("/")[-1]
        c.execute("UPDATE entries SET paths=? WHERE id=?",
                  (json.dumps([{"label":label,"path":row["path"]}]), row["id"]))
    if rows:
        print(f"  [DB] migrated {len(rows)} path→paths")

    # seed entries if empty
    if c.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0:
        seed = [
            ("2.9.10","setup_x64_2.9.10.0-8-\nsetup_x64_2.9.10.0-8","CAS-22528-N2D9",
             "Bangkok (Factory 7)","Delta7 NPI Order requirement for Max height of 2 component groups",
             "","[]","D:\\[1] AOI\\[1] All AOI Software\\KY_AOI_v142_setup_x64_2.9.0.0_AL03-42-g4ce9ef70489(B_v2.9.0.0).exe",
             json.dumps([{"label":"v2.9.0.0 installer","path":"D:\\[1] AOI\\[1] All AOI Software\\KY_AOI_v142_setup_x64_2.9.0.0_AL03-42-g4ce9ef70489(B_v2.9.0.0).exe"}]),
             "bug","AOI"),
            ("2.9.5","setup_x64_2.9.6.0-18-g.exe\nsetup_x64_2.9.6.0-1).exe","CAS-22025-Y4C4",
             "Bangkok (Factory 7)","Delta7 BGA product cannot generate real ball shape",
             "","[]","D:\\[1] AOI\\[1] All AOI Software\\KY_AOI_v142_setup_x64_2.9.1.0_AL03-3-g7d3d29a00e9(B_v2.9.1.0).exe",
             json.dumps([{"label":"v2.9.1.0 installer","path":"D:\\[1] AOI\\[1] All AOI Software\\KY_AOI_v142_setup_x64_2.9.1.0_AL03-3-g7d3d29a00e9(B_v2.9.1.0).exe"}]),
             "fix","AOI"),
            ("2.9.5","setup_x64_2.9.6.0-18.exe\nsetup_x64_2.9.6.0-18).exe","",
             "Bangkok (Factory 8)","Shiny object cannot detect",
             "","[]","D:\\[1] AOI\\[1] All AOI Software\\KY_AOI_v142_setup_x64_2.9.1.0_AL03-3-g7d3d29a00e9(B_v2.9.1.0).exe",
             json.dumps([{"label":"v2.9.1.0 installer","path":"D:\\[1] AOI\\[1] All AOI Software\\KY_AOI_v142_setup_x64_2.9.1.0_AL03-3-g7d3d29a00e9(B_v2.9.1.0).exe"}]),
             "workable","AOI"),
        ]
        c.executemany(
            "INSERT INTO entries(ver,details,case_id,customer,issue,notes,images,path,paths,status,category) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            seed)
        print(f"  [DB] seeded {len(seed)} entries")

    c.commit(); c.close()
    print(f"  [DB]  {os.path.abspath(DB_FILE)}")
    print(f"  [IMG] {os.path.abspath(UPLOADS)}")

# ── Request Handler ───────────────────────────────────────────────────────────

class H(BaseHTTPRequestHandler):

    # ALL mutations go through POST to avoid CORS preflight issues.
    # URL scheme:
    #   POST /api/entries            → create
    #   POST /api/entries/<id>/save  → update
    #   POST /api/entries/<id>/delete→ delete
    #   POST /api/categories         → create
    #   POST /api/categories/<id>/delete → delete
    #   POST /api/upload             → upload image
    #   GET  /api/open?path=...      → open explorer

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Length","0")
        self.end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        qs   = urllib.parse.parse_qs(p.query)
        if path in ("/","/index.html"):
            self._file("index.html","text/html")
        elif path == "/api/entries":
            self._entries(qs)
        elif path == "/api/categories":
            self._cats()
        elif path == "/api/open":
            self._open(qs.get("path",[""])[0])
        elif path.startswith("/uploads/"):
            self._upload_serve(path[9:])
        else:
            self._json({"error":"not found"},404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        print(f"  POST {path}")

        # entries
        if path == "/api/entries":
            self._entry_create(body)
        elif re.match(r"^/api/entries/(\d+)/save$", path):
            m = re.match(r"^/api/entries/(\d+)/save$", path)
            self._entry_update(m.group(1), body)
        elif re.match(r"^/api/entries/(\d+)/delete$", path):
            m = re.match(r"^/api/entries/(\d+)/delete$", path)
            self._entry_delete(m.group(1))
        # categories
        elif path == "/api/categories":
            self._cat_create(body)
        elif re.match(r"^/api/categories/(\d+)/delete$", path):
            m = re.match(r"^/api/categories/(\d+)/delete$", path)
            self._cat_delete(m.group(1))
        # upload
        elif path == "/api/upload":
            self._upload(body)
        # open via POST body
        elif path == "/api/open":
            self._open(body.get("path",""))
        else:
            self._json({"error":"not found"},404)

    # ── Entries ──

    def _entries(self, qs):
        q   = qs.get("q",[""])[0].lower()
        cus = qs.get("customer",[""])[0]
        st  = qs.get("status",[""])[0]
        cat = qs.get("category",[""])[0]
        try:
            c = db()
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM entries ORDER BY ver DESC, id DESC")]
            c.close()
            if q:
                rows = [r for r in rows if q in
                        (r["ver"]+r["details"]+r["case_id"]+r["customer"]+r["issue"]+r["path"]).lower()]
            if cus: rows = [r for r in rows if r["customer"]==cus]
            if st:  rows = [r for r in rows if r.get("status","")==st]
            if cat: rows = [r for r in rows if r.get("category","")==cat]
            self._json({"entries":rows,"total":len(rows)})
        except Exception as e:
            self._json({"error":str(e)},500)

    def _entry_create(self, b):
        try:
            c = db()
            cur = c.execute(
                "INSERT INTO entries(ver,details,case_id,customer,issue,notes,images,path,paths,status,category)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (b.get("ver",""), b.get("details",""), b.get("case_id",""),
                 b.get("customer",""), b.get("issue",""), b.get("notes",""),
                 b.get("images","[]"), b.get("path",""), b.get("paths","[]"),
                 b.get("status","workable"), b.get("category","AOI")))
            c.commit()
            row = dict(c.execute("SELECT * FROM entries WHERE id=?", (cur.lastrowid,)).fetchone())
            c.close()
            print(f"  [DB] created entry id={row['id']}")
            self._json({"success":True,"entry":row},201)
        except Exception as e:
            self._json({"error":str(e)},500)

    def _entry_update(self, eid, b):
        try:
            c = db()
            c.execute(
                "UPDATE entries SET ver=?,details=?,case_id=?,customer=?,issue=?,notes=?,"
                "images=?,path=?,paths=?,status=?,category=? WHERE id=?",
                (b.get("ver",""), b.get("details",""), b.get("case_id",""),
                 b.get("customer",""), b.get("issue",""), b.get("notes",""),
                 b.get("images","[]"), b.get("path",""), b.get("paths","[]"),
                 b.get("status","workable"), b.get("category","AOI"), eid))
            c.commit()
            row = dict(c.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone())
            c.close()
            print(f"  [DB] updated entry id={eid}")
            self._json({"success":True,"entry":row})
        except Exception as e:
            self._json({"error":str(e)},500)

    def _entry_delete(self, eid):
        try:
            c = db()
            c.execute("DELETE FROM entries WHERE id=?", (eid,))
            c.commit(); c.close()
            print(f"  [DB] deleted entry id={eid}")
            self._json({"success":True})
        except Exception as e:
            self._json({"error":str(e)},500)

    # ── Categories ──

    def _cats(self):
        try:
            c = db()
            rows = [dict(r) for r in c.execute("SELECT * FROM categories ORDER BY id")]
            c.close()
            self._json({"categories":rows})
        except Exception as e:
            self._json({"error":str(e)},500)

    def _cat_create(self, b):
        name = b.get("name","").strip()
        if not name:
            return self._json({"error":"name required"},400)
        try:
            c = db()
            c.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (name,))
            c.commit()
            rows = [dict(r) for r in c.execute("SELECT * FROM categories ORDER BY id")]
            c.close()
            self._json({"success":True,"categories":rows})
        except Exception as e:
            self._json({"error":str(e)},500)

    def _cat_delete(self, cid):
        try:
            c = db()
            c.execute("DELETE FROM categories WHERE id=?", (cid,))
            c.commit()
            rows = [dict(r) for r in c.execute("SELECT * FROM categories ORDER BY id")]
            c.close()
            self._json({"success":True,"categories":rows})
        except Exception as e:
            self._json({"error":str(e)},500)

    # ── Upload ──

    def _upload(self, b):
        try:
            fname = re.sub(r"[^a-zA-Z0-9._-]","_", b.get("filename","img.png"))
            base, ext = os.path.splitext(fname)
            ext = ext or ".png"
            unique = f"{base}_{int(time.time()*1000)}{ext}"
            raw = b.get("data","")
            if "," in raw: raw = raw.split(",",1)[1]
            data = base64.b64decode(raw)
            fpath = os.path.join(UPLOADS, unique)
            with open(fpath,"wb") as f: f.write(data)
            print(f"  [IMG] saved {fpath} ({len(data)} bytes)")
            self._json({"success":True,"url":f"/uploads/{unique}","size":len(data)})
        except Exception as e:
            print(f"  [IMG] error: {e}")
            self._json({"error":str(e)},500)

    def _upload_serve(self, fname):
        fpath = os.path.join(UPLOADS, fname)
        try:
            with open(fpath,"rb") as f: data = f.read()
            ext  = fname.rsplit(".",1)[-1].lower()
            mime = {"png":"image/png","jpg":"image/jpeg","jpeg":"image/jpeg",
                    "gif":"image/gif","webp":"image/webp"}.get(ext,"application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type",mime)
            self.send_header("Content-Length",len(data))
            self._cors(); self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404); self.end_headers()

    # ── Open Explorer ──

    def _open(self, path):
        try:
            if not path: raise ValueError("no path")
            path = os.path.normpath(path)
            folder = os.path.dirname(path)

            if os.path.exists(path) and not os.path.isdir(path):
                line2 = f'sh.Run "explorer.exe /select," & Chr(34) & "{path}" & Chr(34), 1, False'
            else:
                line2 = f'sh.Run "explorer.exe " & Chr(34) & "{folder}" & Chr(34), 1, False'

            vbs = "\r\n".join([
                'Set sh = CreateObject("WScript.Shell")',
                line2,
                'WScript.Sleep 1500',
                'sh.AppActivate "File Explorer"',
                'WScript.Sleep 300',
                'sh.AppActivate "File Explorer"',
            ])
            vbs_path = os.path.join(os.environ.get("TEMP","C:\\Windows\\Temp"), "sw_open.vbs")
            with open(vbs_path,"w") as f: f.write(vbs)
            subprocess.Popen(["wscript.exe", vbs_path], creationflags=0x08000000)
            self._json({"success":True})
        except Exception as e:
            try:
                subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
                self._json({"success":True,"fallback":True})
            except Exception as e2:
                self._json({"success":False,"message":str(e2)},500)

    # ── Helpers ──

    def _file(self, fname, mime):
        try:
            with open(fname,"rb") as f: data = f.read()
            self.send_response(200)
            self.send_header("Content-Type",mime)
            self.send_header("Content-Length",len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404); self.end_headers()
            self.wfile.write(b"index.html not found")

    def _body(self):
        n = int(self.headers.get("Content-Length",0))
        if not n: return {}
        try: return json.loads(self.rfile.read(n))
        except: return {}

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",len(body))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")

    def log_message(self, fmt, *args):
        print(f"  [{self.address_string()}] {fmt%args}")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    s = HTTPServer(("localhost", PORT), H)
    print(f"\n✅  http://localhost:{PORT}   Ctrl+C to stop\n")
    try: s.serve_forever()
    except KeyboardInterrupt: print("\nStopped.")
