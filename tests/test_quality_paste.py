# -*- coding: utf-8 -*-
import json
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import quality_probe
from webui.quality_ops import start_quality_scan, quality_status, stop_quality_scan

def test_paste_quality():
    raw_json = json.dumps({
        "accounts": [
            {
                "provider": "grok_build",
                "name": "jade.fox286@ilovemyemail.net",
                "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
                "access_token": "eyJ0eXAiOiJhdCtqd3QiLCJhbGciOiJFUzI1NiIsImtpZCI6Im9hdXRoMi1wcm9kdWN0aW9uLTIwMjYtMDItMTkifQ.test",
                "refresh_token": "3oZmwQ8mq5g4XnHrvH43KF3MYAisXNAJ8H0aNxjOgpAnmmgMzBuEIVjfLa86tEROnqo4suMnzGBiwAyFHlNcUA",
                "email": "jade.fox286@ilovemyemail.net"
            }
        ]
    })

    orig = quality_probe.probe_account
    try:
        quality_probe.probe_account = lambda rec, **kw: {
            "email": rec.get("email"),
            "verdict": "healthy",
            "tps": 35.5,
            "has_thinking": True,
            "duration_ms": 500,
            "output_tokens": 15,
            "error": "",
        }

        res = start_quality_scan(source="paste", raw_input=raw_json, workers=1)
        assert res.get("ok") is True
        assert res.get("total") == 1

        time.sleep(0.5)
        st = quality_status()
        assert st.get("source") == "paste"
        assert len(st.get("items", [])) >= 1
        assert st["items"][0]["email"].startswith("ja")
        assert "ilovemyemail.net" in st["items"][0]["email"]
        assert st["items"][0]["verdict"] == "healthy"
        stop_quality_scan()
        print("ALL PASTE QUALITY TESTS PASSED SUCCESSFULLY!")
    finally:
        quality_probe.probe_account = orig

if __name__ == "__main__":
    test_paste_quality()
