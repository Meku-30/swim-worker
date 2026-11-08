"""試運転用の HTTPS の置き場 (127.0.0.1 だけ)。使い方: serve.py <フォルダ> <ポート> <証明書> <鍵>"""
import functools, http.server, ssl, sys

root, port, crt, key = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=root)
s = http.server.ThreadingHTTPServer(("127.0.0.1", port), h)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(crt, key)
s.socket = ctx.wrap_socket(s.socket, server_side=True)
s.serve_forever()
