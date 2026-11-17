#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Supabase Keep-Alive Ping Scripti (keepalive_ping.py)
----------------------------------------------------
Supabase ücretsiz planının 7 gün hareketsizlik sonrası uyku moduna (Pause)
geçmesini engellemek için bağımsız çalıştırılabilir hafif Python scripti.

Kullanım Alanları:
- Windows Görev Zamanlayıcı (Task Scheduler)
- Linux/macOS crontab
- Manuel veya harici CI/CD tetiklemeleri

Kimlik Bilgisi Çözümleme Sırası:
1. Ortam değişkenleri (SUPABASE_URL, SUPABASE_KEY)
2. .streamlit/secrets.toml dosyası (varsa)
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path
import requests


def _load_credentials_from_secrets_toml() -> tuple[str | None, str | None]:
    """Proje dizinindeki .streamlit/secrets.toml dosyasından SUPABASE bilgilerini okur."""
    base_dir = Path(__file__).resolve().parent
    secrets_path = base_dir / ".streamlit" / "secrets.toml"
    if not secrets_path.exists():
        return None, None

    url_val = None
    key_val = None
    try:
        # Python 3.11+ tomllib desteği
        try:
            import tomllib
            with open(secrets_path, "rb") as f:
                data = tomllib.load(f)
            url_val = data.get("SUPABASE_URL")
            key_val = data.get("SUPABASE_KEY")
        except ImportError:
            # Basit satır bazlı ayrıştırma (harici paket gerektirmemesi için)
            with open(secrets_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k == "SUPABASE_URL":
                            url_val = v
                        elif k == "SUPABASE_KEY":
                            key_val = v
    except Exception as exc:
        print(f"[UYARI] secrets.toml okunurken hata oluştu: {exc}", file=sys.stderr)

    return url_val, key_val


def get_supabase_credentials() -> tuple[str | None, str | None]:
    """Ortam değişkenlerinden veya .streamlit/secrets.toml'dan kimlik bilgilerini alır."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")

    if not (url and key):
        toml_url, toml_key = _load_credentials_from_secrets_toml()
        url = url or toml_url
        key = key or toml_key

    return url, key


def ping_supabase(timeout: int = 10) -> tuple[bool, int, str]:
    """
    Supabase REST API'sine hafif bir SELECT isteği gönderir.
    Dönüş: (başarılı_mı, gecikme_ms, durum_mesajı)
    """
    url, key = get_supabase_credentials()
    if not url or not key:
        return (
            False,
            0,
            "SUPABASE_URL veya SUPABASE_KEY bulunamadı (Ortam değişkeni veya .streamlit/secrets.toml kontrol edin).",
        )

    clean_url = url.rstrip("/")
    endpoint = f"{clean_url}/rest/v1/ory_interest_rate_logs"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    params = {"select": "id", "limit": 1}

    t0 = time.time()
    try:
        response = requests.get(endpoint, headers=headers, params=params, timeout=timeout)
        latency_ms = round((time.time() - t0) * 1000)

        if 200 <= response.status_code < 300:
            return True, latency_ms, f"HTTP {response.status_code} OK ({latency_ms} ms) - Yanıt: {response.text}"
        else:
            return (
                False,
                latency_ms,
                f"HTTP {response.status_code} Hatası ({latency_ms} ms) - Detay: {response.text}",
            )
    except requests.exceptions.Timeout:
        return False, 0, f"Zaman aşımı ({timeout}s). Supabase uykuda olabilir."
    except requests.exceptions.ConnectionError as exc:
        return False, 0, f"Bağlantı hatası (DNS/Sunucu ulaşılamıyor): {exc}"
    except Exception as exc:
        return False, 0, f"Beklenmeyen hata: {exc}"


def main() -> int:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] Supabase Keep-Alive Ping başlatılıyor...")

    success, latency, message = ping_supabase()
    if success:
        print(f"[{now_str}] [BAŞARILI] {message}")
        return 0
    else:
        print(f"[{now_str}] [HATA] {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
