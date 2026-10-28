import sys, os, ssl, urllib.request, urllib.error

url = sys.argv[1]
dest = sys.argv[2]
proxy = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] not in ("", "-") else None

handlers = []
if proxy:
    handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
else:
    handlers.append(urllib.request.ProxyHandler({}))

ctx = ssl.create_default_context()
handlers.append(urllib.request.HTTPSHandler(context=ctx))
opener = urllib.request.build_opener(*handlers)
opener.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")]

try:
    with opener.open(url, timeout=120) as r:
        data = r.read()
    with open(dest, "wb") as f:
        f.write(data)
    print("OK http=%s bytes=%d" % (getattr(r, "status", "?"), len(data)))
except Exception as e:
    print("FAIL %s: %s" % (type(e).__name__, e))
    sys.exit(2)
