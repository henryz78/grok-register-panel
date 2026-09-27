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

            # 4. 验证任务目录下生成了 accounts.txt、sso.txt 以及 summary.json
            task_acc = Path(td) / "accounts.txt"
            task_sso = Path(td) / "sso.txt"
            task_summary = Path(td) / "summary.json"
            assert task_acc.is_file(), "任务目录下应生成 accounts.txt"
            assert task_sso.is_file(), "任务目录下应生成 sso.txt"
            assert task_summary.is_file(), "任务目录下应生成 summary.json 汇总文件"

            import json
            summary_data = json.loads(task_summary.read_text(encoding="utf-8"))
            assert summary_data["total_success"] == 1
            assert summary_data["task_id"] == "task_20260926_test"

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

            # 8. 测试 sso_risk_rejected：任务子目录保持干净（不存失败残留），全局隔离黑名单保留
            app._append_sso_risk_rejected("risk@domain.com", "sso_risk_xyz", "botFlagSource=1")
            assert not (Path(td) / "sso_risk_rejected.txt").exists(), "任务子目录下不应留存失败记录"
            assert (Path(app.ACCOUNTS_DIR) / "sso_risk_rejected.txt").is_file()

            # 9. 测试并发任务目录复用：同一个任务中再次调用 init_task_dir 应复用当前文件夹
            td_reuse = app.init_task_dir()
            assert td_reuse == td, "并发任务或多次初始化应复用同一个任务文件夹"

        finally:
            app.ACCOUNTS_DIR = orig_accounts_dir
            app._current_task_dir = None
            os.environ.pop("GROK_TASK_DIR", None)


def test_concurrent_workers_share_single_task_dir_and_no_failed_records():
    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        orig_accounts_dir = app.ACCOUNTS_DIR
        app.ACCOUNTS_DIR = str(temp_dir / "accounts")
        app._current_task_dir = None
        os.environ.pop("GROK_TASK_DIR", None)

        try:
            # 模拟外部 Supervisor 设置统一任务目录
            shared_task_name = "task_20260927_shared"
            shared_td = app.init_task_dir(task_name=shared_task_name)
            assert os.environ.get("GROK_TASK_DIR") == shared_td

            import threading
            lock = threading.Lock()

            # 模拟 3 个并发 worker 注册
            # W1: 成功 2 个
            # W2: 失败（中途产生临时凭据但不成功，不应落盘到 mail_credentials）
            # W3: 成功 1 个
            def w1_job():
                app.init_task_dir()  # 应该复用 shared_td
                app.save_account_record("w1_1@domain.com", "pwd1", "sso-rw=eyJtoken1; path=/", alock=lock, dev_token="tok1")
                app.save_account_record("w1_2@domain.com", "pwd2", "sso-rw=eyJtoken2; path=/", alock=lock, dev_token="tok2")

            def w2_job():
                app.init_task_dir()
                # worker 2 遇到异常失败，没有调用 save_account_record
                # 只有未成功的临时邮箱，不应写入 mail_credentials
                pass

            def w3_job():
                app.init_task_dir()
                app.save_account_record("w3_1@domain.com", "pwd3", "sso-rw=eyJtoken3; path=/", alock=lock, dev_token="tok3")

            t1 = threading.Thread(target=w1_job)
            t2 = threading.Thread(target=w2_job)
            t3 = threading.Thread(target=w3_job)
            for t in (t1, t2, t3):
                t.start()
            for t in (t1, t2, t3):
                t.join()

            # 验证整个 accounts 下只有一个任务文件夹
            subdirs = [d for d in (temp_dir / "accounts").iterdir() if d.is_dir()]
            assert len(subdirs) == 1, f"应该只有一个共享的任务文件夹，实际: {subdirs}"
            assert subdirs[0].name == shared_task_name

            # 验证 summary.json 正确统计成功总数 3
            import json
            summary = json.loads((subdirs[0] / "summary.json").read_text(encoding="utf-8"))
            assert summary["total_success"] == 3

            # 验证 accounts.txt 包含全部成功账号且只有成功账号
            accs = (subdirs[0] / "accounts.txt").read_text(encoding="utf-8").splitlines()
            assert len(accs) == 3

            # 验证 mail_credentials.txt 也只包含成功注册的邮箱凭证
            creds = (subdirs[0] / "mail_credentials.txt").read_text(encoding="utf-8").splitlines()
            assert len(creds) == 3
            assert any("w1_1@domain.com" in c for c in creds)
            assert any("w3_1@domain.com" in c for c in creds)

        finally:
            app.ACCOUNTS_DIR = orig_accounts_dir
            app._current_task_dir = None
            os.environ.pop("GROK_TASK_DIR", None)


if __name__ == "__main__":
    test_task_directory_and_account_saving()
    test_concurrent_workers_share_single_task_dir_and_no_failed_records()
    print("OK all task directory & concurrency tests passed")
