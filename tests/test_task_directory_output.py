#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Mock camoufox for non-browser testing
if "camoufox" not in sys.modules:
    camoufox = types.ModuleType("camoufox")
    sync_api = types.ModuleType("camoufox.sync_api")
    sync_api.Camoufox = type("Camoufox", (), {})
    sync_api.NewBrowser = type("NewBrowser", (), {})
    camoufox.sync_api = sync_api
    sys.modules["camoufox"] = camoufox
    sys.modules["camoufox.sync_api"] = sync_api

import grok_register_ttk as app


def test_task_directory_and_account_saving():
    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        
        orig_accounts_dir = app.ACCOUNTS_DIR
        app.ACCOUNTS_DIR = str(temp_dir / "accounts")
        app._current_task_dir = None
        
        try:
            # 1. 初始化任务目录
            td = app.init_task_dir(task_name="task_20260926_test")
            assert os.path.isdir(td)
            assert Path(td).name == "task_20260926_test"
            assert app.get_current_task_dir() == td

            # 2. 模拟保存账号
            test_email = "testuser@domain.com"
            test_pwd = "SecretPassword123"
            test_sso = "sso-rw=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.token123; path=/; domain=.x.ai"
            
            app.save_account_record(test_email, test_pwd, test_sso)

            # 3. 验证单账号单独文件 NOT created
            single_file = Path(app.ACCOUNTS_DIR) / f"{test_email}.txt"
            assert not single_file.exists(), f"不应存在单个账号文件: {single_file}"

            # 4. 验证任务目录下生成了 accounts.txt 和 sso.txt
            task_acc = Path(td) / "accounts.txt"
            task_sso = Path(td) / "sso.txt"
            assert task_acc.is_file(), "任务目录下应生成 accounts.txt"
            assert task_sso.is_file(), "任务目录下应生成 sso.txt"

            acc_content = task_acc.read_text(encoding="utf-8")
            assert f"{test_email}----{test_pwd}----{test_sso}" in acc_content
            
            sso_content = task_sso.read_text(encoding="utf-8").strip()
            assert sso_content == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.token123"

            # 5. 验证根目录下也同步追加了汇总
            root_acc = Path(app.ACCOUNTS_DIR) / "accounts.txt"
            root_sso = Path(app.ACCOUNTS_DIR) / "sso.txt"
            assert root_acc.is_file()
            assert root_sso.is_file()
            assert acc_content == root_acc.read_text(encoding="utf-8")
            assert sso_content == root_sso.read_text(encoding="utf-8").strip()

            # 6. 测试 mail_credentials
            app.append_mail_credential(test_email, "mail_token_abc")
            assert (Path(td) / "mail_credentials.txt").is_file()
            assert (Path(app.ACCOUNTS_DIR) / "mail_credentials.txt").is_file()

            # 7. 测试 sso_pending
            app._append_sso_pending("pending@domain.com", "sso_pending_xyz")
            assert (Path(td) / "sso_pending.txt").is_file()
            assert (Path(app.ACCOUNTS_DIR) / "sso_pending.txt").is_file()

            # 8. 测试 sso_risk_rejected
            app._append_sso_risk_rejected("risk@domain.com", "sso_risk_xyz", "botFlagSource=1")
            assert (Path(td) / "sso_risk_rejected.txt").is_file()
            assert (Path(app.ACCOUNTS_DIR) / "sso_risk_rejected.txt").is_file()

        finally:
            app.ACCOUNTS_DIR = orig_accounts_dir
            app._current_task_dir = None


if __name__ == "__main__":
    test_task_directory_and_account_saving()
    print("OK task directory output")
