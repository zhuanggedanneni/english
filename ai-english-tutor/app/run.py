import os
from pathlib import Path

import uvicorn


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))

    # HTTPS：证书存在就自动启用。跨设备演示时浏览器麦克风要求安全上下文
    # （本机 localhost 豁免，手机/平板访问局域网 IP 就必须 https）。
    # 自签证书要先在浏览器点一次"继续访问"；有效期 2 天，过期用 openssl 重签。
    cert = os.getenv("SSL_CERTFILE") or str(Path(__file__).parent / "certs" / "cert.pem")
    key = os.getenv("SSL_KEYFILE") or str(Path(__file__).parent / "certs" / "key.pem")
    ssl_kwargs = {}
    if Path(cert).exists() and Path(key).exists():
        ssl_kwargs = {"ssl_certfile": cert, "ssl_keyfile": key}
        print(f"[https] https://{host}:{port}  证书: {cert}")
    else:
        print(f"[http] http://{host}:{port}  (app/certs/ 下没有证书，未启用 HTTPS)")

    uvicorn.run(
        "backend.main:app",
        host=host,
        port=port,
        reload=os.getenv("RELOAD", "0") == "1",
        **ssl_kwargs,
    )
