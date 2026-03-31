"""埋め込みCA証明書

Redis TLS接続用のCA証明書をコード内に保持する。
外部の ca.crt ファイルが不要になる。
"""
import os
import tempfile

CA_CERT_PEM = """\
-----BEGIN CERTIFICATE-----
MIIFETCCAvmgAwIBAgIUa37Dpr6cCRedc6aFqB0Lxita18UwDQYJKoZIhvcNAQEL
BQAwGDEWMBQGA1UEAwwNc3dpbS1yZWRpcy1jYTAeFw0yNjAzMzEyMTA2MzdaFw0z
NjAzMjgyMTA2MzdaMBgxFjAUBgNVBAMMDXN3aW0tcmVkaXMtY2EwggIiMA0GCSqG
SIb3DQEBAQUAA4ICDwAwggIKAoICAQCRhkoOXWg0ewc/HFxp59EO1nws/g6x+czH
Vbclrwiu5rty1AYcZs7OggqDAi+Uju7eJTvQxhWE2uOk3yYYWT3VcJsD3nblZAuA
i6gi6rIOM47fweVyUAyuFRdGibCTqqvvRye5SQxG6QJa4PZTl/GeAz90MqThES50
jkhSe2esA5TRGNTJI8yshD/JVjCRdu6sPuzK1X9LwDcAJqKCTrPtnAxU0j53ub/r
8gORWwgxFhiY8eRK5TMmENeqcplntx69DC4RenxqnxA8vaF3R40Vsqmufpfvvxph
KlEtzWXCeXznnOTkTnVejVir0gvzQjETcnXp4oQJyEgvBv6DGGJojlbWhlcwMpnY
aLsE74Uq+nS27vlvH1UZlyc++TACqbCvYm9bwVJkUeVcMJRqp3zzXHXmTDqWB7YY
CbPQNuXIwjEiTpEm3SykqaFQhlxEFjpB0u6rQGfWEwB8pF5SiYOruam0rz8x2M6i
jQ9KcOOeo8eKuV1UDwM7P9bCt0EMr3Vd51ttancdWk+GG4YmSf1gHZwmbJuJpBCB
YAiHFptliSRT0IvQ0haILvCz7Fc06g5YSFYGtcFP4UBdbh2yTnsrw78qHnjeb9vi
BszxexdElFAk5xaG1WKl0VYs1FrWdVcngi2BRkS/zSCjILOeBfSxLBGgEGgFv8Gd
GUzuycDUnwIDAQABo1MwUTAdBgNVHQ4EFgQUngQHZ7JSi0eCv0gsvpKwu9p332Qw
HwYDVR0jBBgwFoAUngQHZ7JSi0eCv0gsvpKwu9p332QwDwYDVR0TAQH/BAUwAwEB
/zANBgkqhkiG9w0BAQsFAAOCAgEAEFRbB+Pe1CGzR1kNNgpw2j/OOitB5hm03GhH
W6as1nEaizQxGX+GV5N70yvLYef+ig43iSq7ved04/mCQONCnMD3Og0OGExmOOJ/
ffs0m8c5jLo3Zlvesk2O5iyQPqvYUYT2DnZvZTKc0MW+ab4vsIonpe2GlWZm2kOq
7ryXA+xjuZNXJVeEj9XWnQ6ZxFdv1U2S7c44mGETk571At6qasa24DONNwC/9omB
6cvdm1b28+sxVVZgFC4oZYQIKX0k9emGONcE47R7NKi3ku63vpzqV+uh6+94yzSt
a8QzXEsp2W4bVdEJc6asAI6ATLVn2ULTWdcuJHURLeHj+hcR7N240Z0uMSKqriGx
nnDgs6iUFEq3EGsxl90HqYO0KfabH8mFXVd2sXLBKJxb8Bq6+OcA9cFfgRaFxmrP
y0sZG+mz7jURCrpoijb5qqMGKMwL5b82A9A8BNbuysoo5bICY50PVG4zwOvB6kBT
Wzt7/195TDXVypH9M7DDnMD0XPrsrxQ1ce9Eg7jWdhMe7dPzz0lm9kJefFPyLgiO
HY7SsUQX4NBMjof3S6Cg7+bzJtjiQazJNuJULHslGWd9gbwd9X3k0UGjgeMOMESj
bmKpFYjCVWnobdviueeior9ma52p387KUSydPkArU3gY0UTVBNG/yk/1x1351Ql7
U2NY60E=
-----END CERTIFICATE-----
"""

_temp_cert_path: str | None = None


def get_ca_cert_path() -> str:
    """CA証明書のファイルパスを返す。

    外部ファイル (./ca.crt) があればそれを使い、
    なければ埋め込み証明書を一時ファイルに書き出して返す。
    """
    global _temp_cert_path

    # 外部ファイルがあればそちらを優先
    for candidate in ["./ca.crt", os.path.join(os.path.dirname(__file__), "..", "ca.crt")]:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)

    # 埋め込み証明書を一時ファイルに書き出す
    if _temp_cert_path and os.path.isfile(_temp_cert_path):
        return _temp_cert_path

    fd, path = tempfile.mkstemp(suffix=".pem", prefix="swim-redis-ca-")
    with os.fdopen(fd, "w") as f:
        f.write(CA_CERT_PEM)
    _temp_cert_path = path
    return path
